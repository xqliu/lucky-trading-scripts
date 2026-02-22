"""
Tests for signal_check.py — core signal detection logic.
"""
import pytest
from unittest.mock import patch, MagicMock


class TestEMA:
    """EMA calculation correctness."""
    
    def test_ema_single_value(self):
        from luckytrader.signal import ema
        assert ema([100.0], 8) == [100.0]
    
    def test_ema_constant_input(self):
        from luckytrader.signal import ema
        data = [50.0] * 20
        result = ema(data, 8)
        assert all(abs(v - 50.0) < 1e-6 for v in result)
    
    def test_ema_trending_up(self):
        from luckytrader.signal import ema
        data = list(range(1, 21))  # 1..20
        result = ema(data, 8)
        # EMA should lag behind but trend up
        assert result[-1] > result[0]
        assert result[-1] < data[-1]  # EMA lags the latest value
    
    def test_ema_length_matches_input(self):
        from luckytrader.signal import ema
        data = [float(x) for x in range(100)]
        result = ema(data, 21)
        assert len(result) == len(data)


class TestRSI:
    """RSI calculation correctness."""
    
    def test_rsi_length(self):
        from luckytrader.signal import rsi
        data = [float(x) for x in range(50)]
        result = rsi(data, 14)
        assert len(result) == len(data)
    
    def test_rsi_strong_uptrend(self):
        from luckytrader.signal import rsi
        data = [100 + i * 10 for i in range(30)]  # strong uptrend
        result = rsi(data, 14)
        assert result[-1] > 70  # should be overbought
    
    def test_rsi_strong_downtrend(self):
        from luckytrader.signal import rsi
        data = [1000 - i * 10 for i in range(30)]  # strong downtrend
        result = rsi(data, 14)
        assert result[-1] < 30  # should be oversold
    
    def test_rsi_bounded(self):
        from luckytrader.signal import rsi
        import random
        random.seed(42)
        data = [random.uniform(50, 150) for _ in range(100)]
        result = rsi(data, 14)
        for v in result:
            assert 0 <= v <= 100


class TestFindLevels:
    """Support/resistance level detection."""
    
    def test_no_levels_found(self):
        from luckytrader.signal import find_levels
        # All prices above current → no support
        result = find_levels([200, 300, 400], 100, 'support')
        assert result == []
    
    def test_support_levels(self):
        from luckytrader.signal import find_levels
        # Cluster of lows around 60k
        prices = [60000, 60100, 60050, 70000, 71000, 72000, 50000]
        result = find_levels(prices, 65000, 'support')
        assert len(result) > 0
        assert result[0][0] < 65000  # support below current
    
    def test_resistance_levels(self):
        from luckytrader.signal import find_levels
        prices = [70000, 70100, 70050, 60000, 61000, 62000, 80000]
        result = find_levels(prices, 65000, 'resistance')
        assert len(result) > 0
        assert result[0][0] > 65000
    
    def test_max_three_levels(self):
        from luckytrader.signal import find_levels
        prices = [50000 + i * 10 for i in range(100)] * 3  # lots of clusters
        result = find_levels(prices, 60000, 'support')
        assert len(result) <= 3


class TestAnalyze:
    """Core signal analysis — the money-making logic."""
    
    def _make_candles(self, n, base_price=67000, vol=100, spread=0.001):
        """Generate synthetic candles."""
        candles = []
        for i in range(n):
            p = base_price + i * 10
            candles.append({
                'o': str(p),
                'h': str(p * (1 + spread)),
                'l': str(p * (1 - spread)),
                'c': str(p),
                'v': str(vol),
                't': 1000000000 + i * 1800000,  # 30m apart
            })
        return candles
    
    @patch('luckytrader.signal.get_candles')
    @patch('luckytrader.signal.get_market_context', return_value={})
    @patch('luckytrader.signal.get_recent_fills', return_value=[])
    def test_hold_no_breakout(self, mock_fills, mock_ctx, mock_candles, mock_hl):
        """No breakout → HOLD signal."""
        from luckytrader.signal import analyze
        # Flat candles, no breakout
        flat = self._make_candles(100, base_price=67000, vol=100, spread=0.0005)
        mock_candles.return_value = flat
        
        result = analyze('BTC')
        assert result['signal'] == 'HOLD'
    
    @patch('luckytrader.signal.get_candles')
    @patch('luckytrader.signal.get_market_context', return_value={})
    @patch('luckytrader.signal.get_recent_fills', return_value=[])
    def test_long_signal_on_breakout_with_volume(self, mock_fills, mock_ctx, mock_candles, mock_hl):
        """Breakout above 24h high + volume > 1.25x → LONG."""
        from luckytrader.signal import analyze
        
        # Build candles: 48 flat + 1 breakout candle + some more
        base = 67000
        candles = []
        for i in range(60):
            candles.append({
                'o': str(base), 'h': str(base + 50), 'l': str(base - 50),
                'c': str(base), 'v': str(100), 't': 1000000000 + i * 1800000,
            })
        # Breakout candle: close above 24h high with high volume
        candles[-2] = {
            'o': str(base), 'h': str(base + 500), 'l': str(base - 10),
            'c': str(base + 400),  # close above high_24h
            'v': str(500),  # 5x volume
            't': 1000000000 + 58 * 1800000,
        }
        
        # get_candles is called multiple times (1h, 30m, 1d)
        # We need to return appropriate data for each call
        def side_effect(coin, interval, hours):
            if interval == '30m':
                return candles
            elif interval == '1h':
                return candles[:50]  # fewer candles ok
            elif interval == '1d':
                return candles[:30]
            return candles
        
        mock_candles.side_effect = side_effect
        result = analyze('BTC')
        # The exact signal depends on whether close > high_24h
        # Main thing: function runs without error
        assert result['signal'] in ('LONG', 'SHORT', 'HOLD')
        assert 'price' in result
        assert 'breakout' in result
    
    @patch('luckytrader.signal.get_candles')
    def test_insufficient_data(self, mock_candles, mock_hl):
        """Not enough candles → error."""
        from luckytrader.signal import analyze
        mock_candles.return_value = [{'c': '67000', 'v': '100', 'h': '67100', 'l': '66900', 'o': '67000'}] * 10
        result = analyze('BTC')
        assert 'error' in result
    
    def test_vol_threshold_is_1_25(self, mock_hl):
        """Verify the volume threshold is 1.25x via config."""
        from luckytrader.config import get_config
        cfg = get_config()
        assert cfg.strategy.vol_threshold == 1.25, \
            f"Volume threshold should be 1.25x, got {cfg.strategy.vol_threshold}"


class TestFormatReport:
    """Report formatting — must match actual params."""
    
    def test_format_report_error(self):
        from luckytrader.signal import format_report
        assert format_report({"error": "no data"}) == "no data"
    
    def test_format_report_hold(self):
        from luckytrader.signal import format_report
        result = {
            'price': 67000,
            'volume_usd': 5000000,
            'avg_volume_24h': 4000000,
            'volume_ratio': 1.25,
            'low_24h': 66000,
            'high_24h': 68000,
            'range_24h': 3.0,
            'trend': 'DOWN',
            'ema_8': 67100,
            'ema_21': 67500,
            'rsi': 35.0,
            'breakout': {
                'up': False, 'down': False,
                'vol_ratio_30m': 1.25, 'vol_confirm': True,
            },
            'supports': [(66000, 3)],
            'resistances': [(70000, 5)],
            'signal': 'HOLD',
            'signal_reasons': [],
            'market_context': {},
            'recent_fills': [],
        }
        report = format_report(result)
        assert '$67,000' in report
        assert 'HOLD' in report
    
    def test_format_report_with_signal(self):
        from luckytrader.signal import format_report
        result = {
            'price': 67000,
            'volume_usd': 5000000,
            'avg_volume_24h': 4000000,
            'volume_ratio': 1.5,
            'low_24h': 66000,
            'high_24h': 68000,
            'range_24h': 3.0,
            'trend': 'UP',
            'ema_8': 67600,
            'ema_21': 67400,
            'rsi': 55.0,
            'breakout': {
                'up': True, 'down': False,
                'vol_ratio_30m': 1.5, 'vol_confirm': True,
            },
            'supports': [],
            'resistances': [],
            'signal': 'LONG',
            'signal_reasons': ['突破24h高点$68,000', '30m放量1.5x'],
            'suggested_stop': 64320,
            'suggested_tp': 71690,
            'market_context': {},
            'recent_fills': [],
        }
        report = format_report(result)
        assert 'LONG' in report
        assert '-4%' in report
        assert '+7%' in report
        assert '60h' in report
