"""
Low-hanging fruit tests to improve coverage on backtest, optimize, and monitor modules.
"""
import pytest
import json
from unittest.mock import patch, MagicMock
from io import StringIO
from pathlib import Path


# ============================================================
# backtest.py — print_stats + run_strategy_b edge cases
# ============================================================

class TestBacktestPrintStats:
    """Coverage for print_stats output formatting."""

    def test_print_stats_no_trades(self, capsys):
        from luckytrader.backtest import print_stats
        print_stats("Empty", [])
        out = capsys.readouterr().out
        assert "无交易" in out

    def test_print_stats_with_trades(self, capsys):
        from luckytrader.backtest import print_stats
        trades = [
            {"pnl_pct": 7.0, "reason": "TP", "dir": "LONG", "bars": 10},
            {"pnl_pct": -4.0, "reason": "STOP", "dir": "SHORT", "bars": 5},
            {"pnl_pct": 1.5, "reason": "TIMEOUT", "dir": "LONG", "bars": 48},
        ]
        print_stats("Test", trades)
        out = capsys.readouterr().out
        assert "3笔" in out
        assert "TP1" in out
        assert "SL1" in out
        assert "TO1" in out

    def test_print_stats_all_wins(self, capsys):
        from luckytrader.backtest import print_stats
        trades = [
            {"pnl_pct": 5.0, "reason": "TP", "dir": "LONG", "bars": 10},
            {"pnl_pct": 3.0, "reason": "TP", "dir": "SHORT", "bars": 20},
        ]
        print_stats("AllWin", trades)
        out = capsys.readouterr().out
        assert "胜率100%" in out

    def test_print_stats_all_losses(self, capsys):
        from luckytrader.backtest import print_stats
        trades = [
            {"pnl_pct": -4.0, "reason": "STOP", "dir": "LONG", "bars": 5},
            {"pnl_pct": -4.0, "reason": "STOP", "dir": "SHORT", "bars": 5},
        ]
        print_stats("AllLoss", trades)
        out = capsys.readouterr().out
        assert "胜率0%" in out


class TestRunStrategyB:
    """Edge cases for run_strategy_b."""

    def _make_flat_candles(self, n, price=67000, vol=100):
        return [{
            'o': str(price), 'h': str(price + 10), 'l': str(price - 10),
            'c': str(price), 'v': str(vol), 't': 1000000 + i * 1800000,
        } for i in range(n)]

    def test_no_signals_flat_market(self):
        from luckytrader.backtest import run_strategy_b
        candles = self._make_flat_candles(100)
        trades = run_strategy_b(candles, 0.04, 0.07, 48)
        assert trades == []

    def test_insufficient_candles(self):
        from luckytrader.backtest import run_strategy_b
        candles = self._make_flat_candles(10)
        trades = run_strategy_b(candles, 0.04, 0.07, 48)
        assert trades == []

    def test_vol_thresh_parameter(self):
        from luckytrader.backtest import run_strategy_b
        candles = self._make_flat_candles(100)
        # Very high vol_thresh should produce no trades
        trades = run_strategy_b(candles, 0.04, 0.07, 48, vol_thresh=999)
        assert trades == []


# ============================================================
# optimize.py — run_backtest + simulate_trade
# ============================================================

class TestOptimizeSimulateTrade:
    """Coverage for optimize.simulate_trade (independent copy)."""

    def test_long_stop_loss(self):
        from luckytrader.optimize import simulate_trade
        # Price drops immediately
        highs = [100, 100, 100, 100]
        lows = [100, 90, 90, 90]  # drops to 90 on bar 1
        closes = [100, 91, 91, 91]
        result = simulate_trade('LONG', 100, 0, highs, lows, closes, 0.05, 0.10, 10)
        assert result['reason'] == 'STOP'
        assert result['pnl_pct'] == -5.0

    def test_short_take_profit(self):
        from luckytrader.optimize import simulate_trade
        highs = [100, 95, 88, 88]
        lows = [100, 88, 85, 85]  # drops to 88 on bar 1
        closes = [100, 90, 87, 87]
        result = simulate_trade('SHORT', 100, 0, highs, lows, closes, 0.05, 0.10, 10)
        # TP at 90 (100 * 0.9), low on bar 1 is 88 < 90 → TP
        assert result['reason'] == 'TP'

    def test_timeout(self):
        from luckytrader.optimize import simulate_trade
        # Price stays flat
        highs = [100] * 5
        lows = [100] * 5
        closes = [100] * 5
        result = simulate_trade('LONG', 100, 0, highs, lows, closes, 0.05, 0.10, 3)
        assert result['reason'] == 'TIMEOUT'


class TestOptimizeRunBacktest:
    """Coverage for optimize.run_backtest."""

    def _make_flat(self, n, price=67000, vol=100):
        return [{
            'o': str(price), 'h': str(price + 10), 'l': str(price - 10),
            'c': str(price), 'v': str(vol), 't': 1000000 + i * 1800000,
        } for i in range(n)]

    def test_no_signals(self):
        from luckytrader.optimize import run_backtest
        candles = self._make_flat(100)
        result = run_backtest(candles, 0.04, 0.07, 48)
        assert result['count'] == 0

    def test_returns_dict_keys(self):
        from luckytrader.optimize import run_backtest
        candles = self._make_flat(100)
        result = run_backtest(candles, 0.04, 0.07, 48)
        assert 'count' in result
        assert 'total' in result
        assert 'avg' in result
        assert 'winrate' in result


# ============================================================
# monitor.py — get_account_status, append_check, check_alerts
# ============================================================

class TestMonitorGetAccountStatus:
    """Coverage for monitor.get_account_status."""

    @patch('luckytrader.monitor.requests.post')
    def test_success(self, mock_post):
        from luckytrader.monitor import get_account_status
        mock_post.return_value.json.return_value = {
            "marginSummary": {"accountValue": "250.50"},
            "assetPositions": [{"position": {"coin": "BTC", "szi": "0.001"}}],
        }
        value, positions = get_account_status()
        assert value == 250.50
        assert len(positions) == 1

    @patch('luckytrader.monitor.requests.post', side_effect=Exception("timeout"))
    def test_network_error(self, mock_post):
        from luckytrader.monitor import get_account_status
        value, positions = get_account_status()
        assert value == 100.0  # fallback
        assert positions == []


class TestMonitorAppendCheck:
    """Coverage for monitor.append_check."""

    def test_append_creates_entry(self, tmp_path):
        from luckytrader import monitor
        # Redirect DECISIONS_FILE to tmp
        original = monitor.DECISIONS_FILE
        monitor.DECISIONS_FILE = tmp_path / "DECISIONS.md"
        try:
            prices = {"BTC": 67000.0, "ETH": 2000.0}
            monitor.append_check(prices, 217.76, [], [])
            content = monitor.DECISIONS_FILE.read_text()
            assert "$67,000.00" in content
            assert "HOLD" in content
        finally:
            monitor.DECISIONS_FILE = original

    def test_append_with_alerts(self, tmp_path):
        from luckytrader import monitor
        original = monitor.DECISIONS_FILE
        monitor.DECISIONS_FILE = tmp_path / "DECISIONS.md"
        try:
            prices = {"BTC": 60000.0, "ETH": 1800.0}
            alerts = ["🚨 BTC below support"]
            monitor.append_check(prices, 217.76, [], alerts)
            content = monitor.DECISIONS_FILE.read_text()
            assert "警报" in content
            assert "待分析" in content
        finally:
            monitor.DECISIONS_FILE = original

    def test_append_with_positions(self, tmp_path):
        from luckytrader import monitor
        original = monitor.DECISIONS_FILE
        monitor.DECISIONS_FILE = tmp_path / "DECISIONS.md"
        try:
            prices = {"BTC": 67000.0, "ETH": 2000.0}
            positions = [{"position": {"coin": "BTC", "szi": "0.001"}}]
            monitor.append_check(prices, 217.76, positions, [])
            content = monitor.DECISIONS_FILE.read_text()
            assert "BTC 0.001" in content
        finally:
            monitor.DECISIONS_FILE = original


class TestMonitorWakeLucky:
    """Coverage for monitor.wake_lucky."""

    @patch('luckytrader.monitor.subprocess.run')
    @patch('luckytrader.monitor.shutil.which', return_value='/usr/bin/openclaw')
    def test_wake_sends_event(self, mock_which, mock_run, tmp_path):
        from luckytrader import monitor
        original = monitor.ALERT_FLAG
        monitor.ALERT_FLAG = tmp_path / ".alert_triggered"
        try:
            mock_run.return_value = MagicMock(stdout="ok", returncode=0)
            monitor.wake_lucky(["🚨 BTC alert"], {"BTC": 60000, "ETH": 1800})
            mock_run.assert_called_once()
            assert monitor.ALERT_FLAG.exists()
        finally:
            monitor.ALERT_FLAG = original

    @patch('luckytrader.monitor.subprocess.run', side_effect=Exception("fail"))
    @patch('luckytrader.monitor.shutil.which', return_value='/usr/bin/openclaw')
    def test_wake_handles_failure(self, mock_which, mock_run):
        from luckytrader import monitor
        # Should not raise
        monitor.wake_lucky(["🚨 test"], {"BTC": 60000, "ETH": 1800})
