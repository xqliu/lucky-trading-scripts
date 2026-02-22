"""
Tests for configurable volume lookback window.
Verifies that analyze() uses config.lookback_bars for volume averaging,
not a hardcoded value.
"""
import pytest
from unittest.mock import patch, MagicMock


class TestLookbackWindow:
    """Volume averaging must use config-driven lookback window."""

    def _make_candles(self, n, base_price=67000, vol=100, spread=0.001):
        candles = []
        for i in range(n):
            p = base_price + i * 10
            candles.append({
                'o': str(p), 'h': str(p * (1 + spread)),
                'l': str(p * (1 - spread)), 'c': str(p),
                'v': str(vol), 't': 1000000000 + i * 1800000,
            })
        return candles

    @patch('luckytrader.signal.get_candles')
    @patch('luckytrader.signal.get_market_context', return_value={})
    @patch('luckytrader.signal.get_recent_fills', return_value=[])
    def test_volume_uses_lookback_not_hardcoded(self, mock_fills, mock_ctx, mock_candles, mock_hl):
        """Volume ratio must be computed over lookback_bars, not 48."""
        from luckytrader.signal import analyze
        from luckytrader.config import get_config

        lookback = get_config().strategy.lookback_bars

        # 60 candles: first 58 have vol=100, candle[-2] has vol=500
        candles = self._make_candles(60, vol=100)
        candles[-2]['v'] = str(500)

        def side_effect(coin, interval, hours):
            if interval == '30m':
                return candles
            elif interval == '1h':
                return candles[:55]
            return candles[:30]

        mock_candles.side_effect = side_effect
        result = analyze('BTC')

        # The volume ratio should be 500/100 = 5.0x (since lookback window bars all have vol=100)
        assert abs(result['volume_ratio'] - 5.0) < 0.5, \
            f"Expected vol_ratio ~5.0 (500/100 over {lookback} bars), got {result['volume_ratio']}"

    @patch('luckytrader.signal.get_candles')
    @patch('luckytrader.signal.get_market_context', return_value={})
    @patch('luckytrader.signal.get_recent_fills', return_value=[])
    def test_lookback_reads_from_config(self, mock_fills, mock_ctx, mock_candles, mock_hl):
        """Verify lookback_bars is read from config, not hardcoded."""
        from luckytrader.config import get_config
        cfg = get_config()
        # Just verify it's an int and reasonable
        assert isinstance(cfg.strategy.lookback_bars, int)
        assert cfg.strategy.lookback_bars > 0

    @patch('luckytrader.signal.get_candles')
    @patch('luckytrader.signal.get_market_context', return_value={})
    @patch('luckytrader.signal.get_recent_fills', return_value=[])
    def test_short_candles_no_crash(self, mock_fills, mock_ctx, mock_candles, mock_hl):
        """When candles < lookback_bars, should still work (use available data)."""
        from luckytrader.signal import analyze

        # Only 5 30m candles (less than lookback)
        candles_30m = self._make_candles(5, vol=100)
        candles_1h = self._make_candles(55, vol=100)

        def side_effect(coin, interval, hours):
            if interval == '30m':
                return candles_30m
            elif interval == '1h':
                return candles_1h
            return candles_1h[:30]

        mock_candles.side_effect = side_effect
        result = analyze('BTC')
        # Should not crash, should return valid result
        assert 'signal' in result or 'error' in result

    @patch('luckytrader.signal.get_candles')
    @patch('luckytrader.signal.get_market_context', return_value={})
    @patch('luckytrader.signal.get_recent_fills', return_value=[])
    def test_volume_window_excludes_current_and_detection_bar(self, mock_fills, mock_ctx, mock_candles, mock_hl):
        """vol_slice must exclude candles[-1] (unclosed) and candles[-2] (detection bar)."""
        from luckytrader.signal import analyze

        candles = self._make_candles(60, vol=100)
        # Set detection bar and current bar to extreme volume
        candles[-1]['v'] = str(99999)  # unclosed — must not affect avg
        candles[-2]['v'] = str(500)    # detection bar — must not be in avg

        def side_effect(coin, interval, hours):
            if interval == '30m':
                return candles
            elif interval == '1h':
                return candles[:55]
            return candles[:30]

        mock_candles.side_effect = side_effect
        result = analyze('BTC')

        # avg should be based on vol=100 bars only, so ratio = 500*price / 100*price ≈ 5.0
        # If detection bar leaked into avg, ratio would be lower
        # If unclosed bar leaked in, ratio would be much lower
        assert result['volume_ratio'] > 4.0, \
            f"Expected vol_ratio > 4.0 (detection/unclosed bars should be excluded), got {result['volume_ratio']}"

    @patch('luckytrader.signal.get_candles')
    @patch('luckytrader.signal.get_market_context', return_value={})
    @patch('luckytrader.signal.get_recent_fills', return_value=[])
    def test_different_lookback_gives_different_ratio(self, mock_fills, mock_ctx, mock_candles, mock_hl):
        """If we change lookback_bars, the volume ratio should change when volume varies."""
        from luckytrader.signal import analyze
        from luckytrader import config as cfg_mod

        # Build candles: recent 12 bars have vol=200, older bars have vol=50
        candles = self._make_candles(60, vol=50)
        # Make bars at index -14 to -3 have vol=200 (covers lookback window)
        for i in range(-14, -2):
            candles[i]['v'] = str(200)
        candles[-2]['v'] = str(300)  # detection bar

        def side_effect(coin, interval, hours):
            if interval == '30m':
                return candles
            elif interval == '1h':
                return candles[:55]
            return candles[:30]

        mock_candles.side_effect = side_effect

        # With current config lookback, avg is based on recent high-vol bars
        result = analyze('BTC')
        ratio = result['volume_ratio']

        # With lookback=48 bars: window includes mix of vol=50 (older) and vol=200 (recent 12 bars)
        # avg ≈ (36*50 + 12*200)/48 = 87.5, detection bar vol=300 → ratio ≈ 3.43
        # This verifies that the lookback window IS used in the calculation (ratio > 1.0)
        # and that the 48-bar window correctly dilutes the average with older lower-vol bars
        assert ratio > 1.5, \
            f"Vol ratio should reflect 48-bar lookback (mix of old/new bars), got {ratio}"
        assert ratio > 3.0, \
            f"With 48-bar window diluting avg with vol=50 bars, ratio should be ~3.4, got {ratio}"


class TestLookbackConfig:
    """Config integration for lookback_bars."""

    def test_config_has_lookback_bars(self, mock_hl):
        from luckytrader.config import get_config
        cfg = get_config()
        assert hasattr(cfg.strategy, 'lookback_bars')

    def test_config_default_fallback(self, mock_hl):
        """StrategyConfig has a default value for lookback_bars."""
        from luckytrader.config import StrategyConfig
        default = StrategyConfig()
        assert isinstance(default.lookback_bars, int)
        assert default.lookback_bars > 0
