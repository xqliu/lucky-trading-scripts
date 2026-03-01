"""
Critical Path Tests — 验证所有关键交易逻辑正确性
================================================
覆盖：
1. place_market_order 调用签名一致性
2. early validation 平仓流程
3. regime TP/SL 覆盖 config fallback
4. signal.py detect_signal 与 strategy.py 一致
5. open_position 参数传递链
6. trailing stop 平仓调用
"""
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
import inspect
import ast
import os


# ─── Test 1: place_market_order 签名验证 ───
class TestPlaceMarketOrderSignature:
    """确保所有调用点的参数顺序与函数签名匹配"""

    def test_function_signature(self):
        """place_market_order(coin, is_buy, size) — 固定签名"""
        # Import the raw module to get unwrapped signature
        import importlib
        spec = importlib.util.spec_from_file_location(
            "trade_raw",
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         'luckytrader', 'trade.py'))
        # Just check the source directly
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        trade_src = open(os.path.join(base, 'luckytrader', 'trade.py')).read()
        assert 'def place_market_order(coin: str, is_buy: bool, size: float)' in trade_src, \
            "place_market_order signature must be (coin: str, is_buy: bool, size: float)"

    def test_all_call_sites_match_signature(self):
        """扫描所有 .py 文件，确保 place_market_order 调用参数正确"""
        issues = []
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        
        for root, dirs, files in os.walk(os.path.join(base, 'luckytrader')):
            dirs[:] = [d for d in dirs if d != '__pycache__']
            for f in files:
                if not f.endswith('.py'):
                    continue
                path = os.path.join(root, f)
                try:
                    tree = ast.parse(open(path).read())
                except:
                    continue
                for node in ast.walk(tree):
                    if isinstance(node, ast.Call):
                        # Check if it's place_market_order
                        func = node.func
                        name = None
                        if isinstance(func, ast.Name) and func.id == 'place_market_order':
                            name = func.id
                        elif isinstance(func, ast.Attribute) and func.attr == 'place_market_order':
                            name = func.attr
                        if name:
                            # Check: no keyword 'reduce_only' (doesn't exist)
                            kw_names = [kw.arg for kw in node.keywords]
                            if 'reduce_only' in kw_names:
                                issues.append(f"{path}:{node.lineno} — reduce_only kwarg (doesn't exist)")
                            # Check: exactly 3 positional args if no keywords
                            if not node.keywords and len(node.args) != 3:
                                issues.append(f"{path}:{node.lineno} — expected 3 args, got {len(node.args)}")
                            # Check: no duplicate keyword args
                            if len(kw_names) != len(set(kw_names)):
                                issues.append(f"{path}:{node.lineno} — duplicate keyword args")
        
        assert not issues, f"place_market_order call issues:\n" + "\n".join(issues)


# ─── Test 2: Early Validation 平仓逻辑 ───
class TestEarlyValidation:
    """验证 ws_monitor 中 early validation 的平仓调用正确"""

    def test_early_exit_uses_close_and_cleanup(self):
        """Early validation 失败 → 使用统一的 close_and_cleanup() 平仓"""
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        ws_path = os.path.join(base, 'luckytrader', 'ws_monitor.py')
        content = open(ws_path).read()
        
        # Should use close_and_cleanup (unified close function)
        assert 'close_and_cleanup(' in content, \
            "Early validation should use close_and_cleanup() for closing"
        
        # Should NOT have raw place_market_order calls in early validation section
        # (only close_and_cleanup should handle the close logic)
        assert 'place_market_order(coin, size, is_buy=' not in content, \
            "Old buggy call pattern still exists!"
        assert 'reduce_only=True' not in content, \
            "reduce_only parameter should not appear in ws_monitor"


# ─── Test 3: Regime TP/SL 覆盖验证 ───
class TestRegimeOverride:
    """验证 open_position 使用 regime 参数而非 config fallback"""

    @patch('luckytrader.execute.get_position')
    @patch('luckytrader.execute.get_market_price')
    @patch('luckytrader.execute.compute_de')
    @patch('luckytrader.execute.get_candles')
    def test_regime_tp_overrides_config(self, mock_candles, mock_de, mock_price, mock_pos):
        """DE > 0.25 → trend → TP=7%, not config fallback"""
        from luckytrader.regime import get_regime_params
        from luckytrader.config import get_config
        
        cfg = get_config()
        
        # Trend regime
        params = get_regime_params(0.35, cfg)
        assert params['tp_pct'] == 0.07, f"Trend TP should be 7%, got {params['tp_pct']*100}%"
        assert params['sl_pct'] == 0.04, f"Trend SL should be 4%, got {params['sl_pct']*100}%"
        assert params['regime'] == 'trend'
        
        # Range regime  
        params = get_regime_params(0.15, cfg)
        assert params['tp_pct'] == 0.02, f"Range TP should be 2%, got {params['tp_pct']*100}%"
        assert params['sl_pct'] == 0.05, f"Range SL should be 5%, got {params['sl_pct']*100}%"
        assert params['regime'] == 'range'
        
        # Unknown (None) → range params (fail-safe)
        params = get_regime_params(None, cfg)
        assert params['tp_pct'] == 0.02
        assert params['regime'] == 'unknown'

    def test_de_threshold_is_025(self):
        """DE threshold must be 0.25 (validated value)"""
        from luckytrader.config import get_config
        cfg = get_config()
        assert cfg.strategy.de_threshold == 0.25


# ─── Test 4: Signal 一致性 ───
class TestSignalConsistency:
    """验证 signal.py 使用 strategy.detect_signal()"""

    def test_signal_py_calls_detect_signal(self):
        """signal.py analyze() must call strategy.detect_signal()"""
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        signal_path = os.path.join(base, 'luckytrader', 'signal.py')
        content = open(signal_path).read()
        
        assert 'from luckytrader.strategy import' in content
        assert 'detect_signal' in content
        # The actual call
        assert 'detect_signal(candles_30m, candles_4h' in content, \
            "analyze() must call detect_signal with candles_30m and candles_4h"


# ─── Test 5: No Silent Exception Handlers in Critical Paths ───
class TestNoSilentExceptions:
    """确保关键文件没有 bare except: pass"""

    def test_no_bare_except_pass_in_critical_files(self):
        """execute.py, trade.py, ws_monitor.py 不允许 bare except: pass"""
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        critical_files = ['execute.py', 'trade.py', 'ws_monitor.py']
        
        issues = []
        for fname in critical_files:
            path = os.path.join(base, 'luckytrader', fname)
            try:
                tree = ast.parse(open(path).read())
            except:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ExceptHandler):
                    # Check body is just 'pass' with no logging
                    if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                        # Check if there's a comment justifying it (we can't check comments via AST)
                        # But at minimum, bare pass without any context is bad
                        has_any_call = False
                        for child in ast.walk(node):
                            if isinstance(child, ast.Call):
                                has_any_call = True
                        if not has_any_call:
                            issues.append(f"{fname}:{node.lineno}")
        
        # Allow specific known-acceptable bare passes (cleanup, socket close)
        # Get actual line numbers by scanning for known patterns
        acceptable_patterns = {
            'ws_monitor.py': ['await self.websocket.close()'],  # websocket close cleanup
            'execute.py': ['lock_path.unlink(', '_COOLDOWN_SECONDS'],  # lock cleanup, cooldown
            'trade.py': ['Allow import without config'],  # config load for testing
        }
        # Just allow the known-acceptable ones by line number
        acceptable = set()
        for fname in critical_files:
            path = os.path.join(base, 'luckytrader', fname)
            lines = open(path).readlines()
            for pattern_list in acceptable_patterns.get(fname, []):
                for i, line in enumerate(lines):
                    if pattern_list in line:
                        # Find the except handler near this line
                        for j in range(i, min(i+5, len(lines))):
                            if 'except' in lines[j]:
                                acceptable.add(f"{fname}:{j+1}")
                                acceptable.add(f"{fname}:{j+2}")
                                break
        # Also accept these specific known-safe ones
        for issue in list(issues):
            # Check if nearby code has a justifying comment
            pass
        # Hardcode accepted after manual review
        known_acceptable = set()
        for i in issues:
            fname, lineno = i.split(':')
            lineno = int(lineno)
            path = os.path.join(base, 'luckytrader', fname)
            lines = open(path).readlines()
            context = ''.join(lines[max(0,lineno-4):lineno+2])
            if any(x in context for x in ['unlink(', 'websocket.close()', 
                    'Allow import', '_COOLDOWN_SECONDS', 'socket 可能已断开']):
                known_acceptable.add(i)
        actual_issues = [i for i in issues if i not in known_acceptable]
        
        assert not actual_issues, \
            f"Bare 'except: pass' in critical files:\n" + "\n".join(actual_issues)


# ─── Test 6: Config consistency ───
class TestConfigConsistency:
    """验证 config.toml 关键参数"""

    def test_max_hold_hours(self):
        from luckytrader.config import get_config
        cfg = get_config()
        assert cfg.risk.max_hold_hours == 60, \
            f"max_hold_hours should be 60, got {cfg.risk.max_hold_hours}"

    def test_tp_is_fallback_only(self):
        """config TP is fallback; regime handles actual TP"""
        from luckytrader.config import get_config
        cfg = get_config()
        # TP should be range default (2%) as safe fallback
        assert cfg.risk.take_profit_pct <= 0.02, \
            f"Config TP should be ≤2% (fallback), got {cfg.risk.take_profit_pct*100}%"

    def test_sl_is_4pct(self):
        from luckytrader.config import get_config
        cfg = get_config()
        assert cfg.risk.stop_loss_pct == 0.04

    def test_vol_threshold(self):
        from luckytrader.config import get_config
        cfg = get_config()
        assert cfg.strategy.vol_threshold == 1.25

    def test_range_and_lookback_bars(self):
        from luckytrader.config import get_config
        cfg = get_config()
        assert cfg.strategy.range_bars == 48
        assert cfg.strategy.lookback_bars == 48

    def test_early_validation_params(self):
        from luckytrader.config import get_config
        cfg = get_config()
        assert cfg.strategy.early_validation_bars == 2
        assert cfg.strategy.early_validation_mfe == 0.8


# ─── Test 7: Regime tighten_only logic ───
class TestRegimeTightenOnly:
    """验证 should_tighten_tp 只收紧不放松"""

    def test_tighten_trend_to_range(self):
        from luckytrader.strategy import should_tighten_tp
        from luckytrader.config import get_config
        cfg = get_config()
        # Old TP=7% (trend), new DE=0.1 (range) → should tighten to 2%
        result = should_tighten_tp(0.07, 0.1, cfg)
        assert result == 0.02

    def test_no_expand_range_to_trend(self):
        from luckytrader.strategy import should_tighten_tp
        from luckytrader.config import get_config
        cfg = get_config()
        # Old TP=2% (range), new DE=0.5 (trend) → should NOT expand
        result = should_tighten_tp(0.02, 0.5, cfg)
        assert result is None

    def test_none_de_no_change(self):
        from luckytrader.strategy import should_tighten_tp
        from luckytrader.config import get_config
        cfg = get_config()
        result = should_tighten_tp(0.07, None, cfg)
        assert result is None


# ─── Test 8: Single Source of Truth for indicators ───
class TestNoIndicatorDuplication:
    """
    铁律：指标计算（EMA、BB、RSI 等）只能在 strategy.py 定义一次。
    其他文件（backtest、signal、execute）必须 import，不准重复实现。
    违反这条 = 回测和实盘逻辑分裂，是最严重的结构性 bug。
    """

    INDICATOR_FUNCTIONS = ['ema', 'rsi', 'bollinger', 'detect_signal',
                           'get_trend_4h', 'get_range_levels', 'get_vol_ratio',
                           'should_tighten_tp']

    def test_no_indicator_redefinition_in_non_strategy_files(self):
        """确保指标函数只在 strategy.py 中定义，其他文件不得重新定义"""
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        pkg_dir = os.path.join(base, 'luckytrader')

        violations = []
        for fname in os.listdir(pkg_dir):
            if not fname.endswith('.py') or fname == 'strategy.py':
                continue
            path = os.path.join(pkg_dir, fname)
            try:
                tree = ast.parse(open(path).read())
            except:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name in self.INDICATOR_FUNCTIONS:
                    violations.append(f"{fname}:{node.lineno} redefines '{node.name}'")

        assert not violations, (
            "Indicator functions must only be defined in strategy.py. "
            "Other files must `from luckytrader.strategy import ...`.\n"
            "Violations:\n" + "\n".join(violations)
        )

    def test_backtest_imports_detect_signal_from_strategy(self):
        """回测必须使用 strategy.detect_signal()，不准自己算信号"""
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        backtest_path = os.path.join(base, 'luckytrader', 'backtest.py')
        if not os.path.exists(backtest_path):
            return  # no backtest file yet

        source = open(backtest_path).read()
        # Must import detect_signal from strategy (or from signal which re-exports it)
        has_import = ('from luckytrader.strategy import' in source and 'detect_signal' in source) or \
                     ('from luckytrader.signal import' in source and 'detect_signal' in source) or \
                     ('from .strategy import' in source and 'detect_signal' in source) or \
                     ('from .signal import' in source and 'detect_signal' in source)
        assert has_import, (
            "backtest.py must import detect_signal from strategy.py or signal.py, "
            "not reimplement signal logic"
        )

    def test_signal_imports_from_strategy(self):
        """signal.py 的指标必须来自 strategy.py"""
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        signal_path = os.path.join(base, 'luckytrader', 'signal.py')
        source = open(signal_path).read()

        assert 'from luckytrader.strategy import' in source or \
               'from .strategy import' in source, \
            "signal.py must import indicators from strategy.py"


# ─── Test 9: Chain-first safety ───
class TestChainFirstSafety:
    """
    铁律：任何改变仓位的重试逻辑，每次重试前必须查链上状态。
    防止 emergency_close 重复执行导致反向开仓。
    """

    def test_emergency_close_checks_position_before_retry(self):
        """emergency_close 必须在重试前调用 get_position"""
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        source = open(os.path.join(base, 'luckytrader', 'execute.py')).read()

        # Find emergency_close function body
        import re
        match = re.search(r'def emergency_close\(.*?\n(.*?)(?=\ndef |\Z)',
                          source, re.DOTALL)
        assert match, "emergency_close function not found"
        body = match.group(1)

        assert 'get_position' in body, (
            "emergency_close() must call get_position() to verify chain state "
            "before each retry. This prevents accidental reverse positions."
        )
