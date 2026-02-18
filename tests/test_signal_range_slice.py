"""
Unit tests for signal.py range_slice logic.
Verifies that the breakout candle is excluded from the range calculation.
"""
from unittest.mock import patch, MagicMock
import pytest


def _make_candle(o, h, l, c, v=100, t=None):
    """Helper to create a candle dict."""
    return {"o": str(o), "h": str(h), "l": str(l), "c": str(c), "v": str(v), "T": t or 0}


class TestRangeSliceExcludesBreakoutCandle:
    """range_slice must NOT include the breakout candle (candles_30m[-2])."""

    def test_breakout_candle_excluded_from_range(self):
        """If breakout candle defines the range boundary, breakout should still be detected.

        Scenario: 12 range candles trade 100-200, then breakout candle drops to 50.
        Old bug: breakout candle (low=50) was IN the range → low_range=50 → 50 < 50 = False.
        Fix: breakout candle excluded → low_range=100 → 50 < 100 = True.
        """
        from luckytrader.signal import analyze

        # Build candles: 14 range candles + 1 breakout candle + 1 current candle
        range_candles = [_make_candle(150, 200, 100, 150, v=10) for _ in range(14)]
        breakout_candle = _make_candle(150, 160, 50, 55, v=500)  # massive drop with volume
        current_candle = _make_candle(55, 60, 50, 55, v=10)

        candles_30m = range_candles + [breakout_candle, current_candle]

        # 1h candles for EMA/RSI (need >= 50)
        candles_1h = [_make_candle(150, 200, 100, 150, v=10) for _ in range(60)]

        # 1d candles for support/resistance
        candles_1d = [_make_candle(150, 200, 100, 150) for _ in range(10)]

        # Mock all API calls
        def mock_get_candles(coin, interval, hours):
            if interval == '30m':
                return candles_30m
            elif interval == '1h':
                return candles_1h
            elif interval == '1d':
                return candles_1d
            return []

        mock_config = MagicMock()
        mock_config.strategy.vol_threshold = 1.25
        mock_config.strategy.lookback_bars = 12
        mock_config.strategy.range_bars = 12
        mock_config.risk.stop_loss_pct = 0.04
        mock_config.risk.take_profit_pct = 0.07

        with patch('luckytrader.signal.get_candles', side_effect=mock_get_candles), \
             patch('luckytrader.signal.get_config', return_value=mock_config), \
             patch('luckytrader.signal.get_market_context', return_value={}), \
             patch('luckytrader.signal.get_recent_fills', return_value=[]):

            result = analyze('BTC')

        # Breakout candle low=50 < range low=100 → SHORT signal
        assert result['signal'] == 'SHORT', \
            f"Expected SHORT but got {result['signal']} — breakout candle may be in range"
        assert result['low_24h'] == 100.0, \
            f"low_range should be 100 (from range candles), got {result['low_24h']}"

    def test_no_breakout_when_within_range(self):
        """No breakout when price stays within the range."""
        from luckytrader.signal import analyze

        range_candles = [_make_candle(150, 200, 100, 150, v=10) for _ in range(14)]
        within_range_candle = _make_candle(150, 180, 120, 160, v=500)  # high volume but no breakout
        current_candle = _make_candle(160, 165, 155, 160, v=10)

        candles_30m = range_candles + [within_range_candle, current_candle]
        candles_1h = [_make_candle(150, 200, 100, 150, v=10) for _ in range(60)]
        candles_1d = [_make_candle(150, 200, 100, 150) for _ in range(10)]

        def mock_get_candles(coin, interval, hours):
            if interval == '30m':
                return candles_30m
            elif interval == '1h':
                return candles_1h
            elif interval == '1d':
                return candles_1d
            return []

        mock_config = MagicMock()
        mock_config.strategy.vol_threshold = 1.25
        mock_config.strategy.lookback_bars = 12
        mock_config.strategy.range_bars = 12
        mock_config.risk.stop_loss_pct = 0.04
        mock_config.risk.take_profit_pct = 0.07

        with patch('luckytrader.signal.get_candles', side_effect=mock_get_candles), \
             patch('luckytrader.signal.get_config', return_value=mock_config), \
             patch('luckytrader.signal.get_market_context', return_value={}), \
             patch('luckytrader.signal.get_recent_fills', return_value=[]):

            result = analyze('BTC')

        assert result['signal'] == 'HOLD', \
            f"Expected HOLD but got {result['signal']} — price is within range"

    def test_upside_breakout_detected(self):
        """Upside breakout with volume should produce LONG signal."""
        from luckytrader.signal import analyze

        range_candles = [_make_candle(150, 200, 100, 150, v=10) for _ in range(14)]
        breakout_candle = _make_candle(190, 250, 180, 240, v=500)  # breaks above 200
        current_candle = _make_candle(240, 245, 235, 240, v=10)

        candles_30m = range_candles + [breakout_candle, current_candle]
        candles_1h = [_make_candle(150, 200, 100, 150, v=10) for _ in range(60)]
        candles_1d = [_make_candle(150, 200, 100, 150) for _ in range(10)]

        def mock_get_candles(coin, interval, hours):
            if interval == '30m':
                return candles_30m
            elif interval == '1h':
                return candles_1h
            elif interval == '1d':
                return candles_1d
            return []

        mock_config = MagicMock()
        mock_config.strategy.vol_threshold = 1.25
        mock_config.strategy.lookback_bars = 12
        mock_config.strategy.range_bars = 12
        mock_config.risk.stop_loss_pct = 0.04
        mock_config.risk.take_profit_pct = 0.07

        with patch('luckytrader.signal.get_candles', side_effect=mock_get_candles), \
             patch('luckytrader.signal.get_config', return_value=mock_config), \
             patch('luckytrader.signal.get_market_context', return_value={}), \
             patch('luckytrader.signal.get_recent_fills', return_value=[]):

            result = analyze('BTC')

        assert result['signal'] == 'LONG', \
            f"Expected LONG but got {result['signal']}"
        assert result['high_24h'] == 200.0, \
            f"high_range should be 200, got {result['high_24h']}"

    def test_no_signal_without_volume(self):
        """Breakout without volume confirmation should NOT trigger signal.

        vol_confirm = False direction: breakout occurs but volume is below threshold.
        """
        from luckytrader.signal import analyze

        # Range candles with normal volume=100
        range_candles = [_make_candle(150, 200, 100, 150, v=100) for _ in range(14)]
        # Breakout candle: low=50 (breaks below range low=100) but volume=100 (same as average → ratio=1.0 < 1.25)
        breakout_candle = _make_candle(150, 160, 50, 55, v=100)
        current_candle = _make_candle(55, 60, 50, 55, v=10)

        candles_30m = range_candles + [breakout_candle, current_candle]
        candles_1h = [_make_candle(150, 200, 100, 150, v=100) for _ in range(60)]
        candles_1d = [_make_candle(150, 200, 100, 150) for _ in range(10)]

        def mock_get_candles(coin, interval, hours):
            if interval == '30m':
                return candles_30m
            elif interval == '1h':
                return candles_1h
            elif interval == '1d':
                return candles_1d
            return []

        mock_config = MagicMock()
        mock_config.strategy.vol_threshold = 1.25
        mock_config.strategy.lookback_bars = 12
        mock_config.strategy.range_bars = 12
        mock_config.risk.stop_loss_pct = 0.04
        mock_config.risk.take_profit_pct = 0.07

        with patch('luckytrader.signal.get_candles', side_effect=mock_get_candles), \
             patch('luckytrader.signal.get_config', return_value=mock_config), \
             patch('luckytrader.signal.get_market_context', return_value={}), \
             patch('luckytrader.signal.get_recent_fills', return_value=[]):

            result = analyze('BTC')

        # Breakout happened (low=50 < range low=100) but no volume confirmation
        assert result['breakout']['down'] is True, "Breakout should be detected"
        assert result['breakout']['vol_confirm'] is False, "Volume should NOT confirm"
        assert result['signal'] == 'HOLD', \
            f"Expected HOLD (no volume) but got {result['signal']}"
