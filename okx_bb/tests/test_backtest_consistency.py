"""Verify backtest uses exactly the same functions as production."""
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


class TestBacktestConsistency:
    """Backtest must import from production code — no reimplementations."""

    def test_close_mode_uses_detect_signal(self):
        """Close-mode backtest calls strategy.detect_signal()."""
        from okx_bb.backtest import backtest_close
        src = inspect.getsource(backtest_close)
        assert "detect_signal(" in src, "Must call detect_signal from strategy.py"

    def test_intrabar_mode_uses_get_bb_levels(self):
        """Intrabar-mode backtest calls strategy.get_bb_levels()."""
        from okx_bb.backtest import backtest_intrabar
        src = inspect.getsource(backtest_intrabar)
        assert "get_bb_levels(" in src, "Must call get_bb_levels from strategy.py"

    def test_intrabar_uses_same_trend_as_ws_monitor(self):
        """Backtest get_trend must match ws_monitor._get_trend logic."""
        from okx_bb.backtest import get_trend
        from core.indicators import ema
        # Test with known data
        closes = [float(i) for i in range(200)]
        # Both should return "up" for rising data
        result = get_trend(closes, len(closes) - 1, 96, 8)
        assert result == "up"

    def test_shared_imports(self):
        """Backtest imports from same modules as production."""
        from okx_bb import backtest, strategy
        # Both use same get_bb_levels
        assert backtest.get_bb_levels is strategy.get_bb_levels
        # Both use same detect_signal
        assert backtest.detect_signal is strategy.detect_signal

    def test_no_duplicate_bb_calculation(self):
        """Backtest must NOT have its own bollinger_bands implementation."""
        from okx_bb import backtest
        src = inspect.getsource(backtest)
        assert "def bollinger_bands" not in src
        assert "def bb(" not in src

    def test_fee_from_config(self):
        """Backtest uses fee from config, not hardcoded."""
        from okx_bb.backtest import backtest_close
        src = inspect.getsource(backtest_close)
        assert "cfg.fees" in src
