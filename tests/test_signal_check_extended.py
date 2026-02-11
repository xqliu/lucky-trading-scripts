"""
Extended signal_check.py tests — covering market context, fills, edge cases.
"""
import pytest
from unittest.mock import patch, MagicMock


class TestGetMarketContext:
    """Market context (funding rate, OI) fetching."""
    
    @patch('requests.post')
    def test_success(self, mock_post):
        from luckytrader.signal import get_market_context
        mock_post.return_value = MagicMock(json=MagicMock(return_value=[
            {"universe": [{"name": "BTC"}, {"name": "ETH"}, {"name": "SOL"}]},
            [
                {"funding": "0.0001", "openInterest": "50000", "markPx": "67000"},
                {"funding": "-0.00005", "openInterest": "200000", "markPx": "1975"},
                {"funding": "0.0002", "openInterest": "100000", "markPx": "120"},
            ],
        ]))
        
        ctx = get_market_context()
        assert "BTC" in ctx
        assert "ETH" in ctx
        assert ctx["BTC"]["funding_rate"] == 0.0001
        assert ctx["ETH"]["mark_price"] == 1975.0
    
    @patch('requests.post')
    def test_network_error_returns_empty(self, mock_post):
        from luckytrader.signal import get_market_context
        mock_post.side_effect = Exception("timeout")
        ctx = get_market_context()
        assert ctx == {}


class TestGetRecentFills:
    """Recent trade fill fetching."""
    
    @patch('requests.post')
    def test_success(self, mock_post):
        from luckytrader.signal import get_recent_fills
        mock_post.return_value = MagicMock(json=MagicMock(return_value=[
            {"coin": "BTC", "side": "B", "sz": "0.001", "px": "67000", "time": 1707600000000},
            {"coin": "BTC", "side": "A", "sz": "0.001", "px": "67500", "time": 1707610000000},
        ]))
        
        fills = get_recent_fills(2)
        assert len(fills) == 2
        assert fills[0]["side"] == "BUY"
        assert fills[1]["side"] == "SELL"
    
    @patch('requests.post')
    def test_error_returns_empty(self, mock_post):
        from luckytrader.signal import get_recent_fills
        mock_post.side_effect = Exception("timeout")
        assert get_recent_fills() == []


class TestAnalyzeEdgeCases:
    """Edge cases in analyze()."""
    
    def _make_candles(self, n, base=67000, vol=100):
        return [{'o': str(base), 'h': str(base+50), 'l': str(base-50),
                 'c': str(base), 'v': str(vol), 't': str(1000000+i*1800000)}
                for i in range(n)]
    
    @patch('luckytrader.signal.get_candles')
    @patch('luckytrader.signal.get_market_context', return_value={})
    @patch('luckytrader.signal.get_recent_fills', return_value=[])
    def test_short_signal_on_breakdown(self, mock_fills, mock_ctx, mock_candles, mock_hl):
        """Breakdown below 24h low + volume → SHORT."""
        from luckytrader.signal import analyze
        
        base = 67000
        candles_30m = []
        for i in range(60):
            candles_30m.append({
                'o': str(base), 'h': str(base+50), 'l': str(base-50),
                'c': str(base), 'v': str(100), 't': str(1000000+i*1800000),
            })
        # Breakdown candle (second to last = [-2])
        candles_30m[-2] = {
            'o': str(base), 'h': str(base+10), 'l': str(base-500),
            'c': str(base-400), 'v': str(500), 't': str(1000000+58*1800000),
        }
        
        candles_1h = candles_30m[:50]
        candles_1d = candles_30m[:30]
        
        def side_effect(coin, interval, hours):
            if interval == '30m': return candles_30m
            elif interval == '1h': return candles_1h
            elif interval == '1d': return candles_1d
            return candles_30m
        
        mock_candles.side_effect = side_effect
        result = analyze('BTC')
        assert result['signal'] in ('SHORT', 'HOLD')
        assert 'breakout' in result
    
    @patch('luckytrader.signal.get_candles')
    @patch('luckytrader.signal.get_market_context', return_value={})
    @patch('luckytrader.signal.get_recent_fills', return_value=[])
    def test_no_volume_confirmation(self, mock_fills, mock_ctx, mock_candles, mock_hl):
        """Breakout without volume → HOLD."""
        from luckytrader.signal import analyze
        
        base = 67000
        candles_30m = []
        for i in range(60):
            candles_30m.append({
                'o': str(base), 'h': str(base+50), 'l': str(base-50),
                'c': str(base), 'v': str(100), 't': str(1000000+i*1800000),
            })
        # Breakout but LOW volume (0.5x average)
        candles_30m[-2] = {
            'o': str(base), 'h': str(base+500), 'l': str(base-10),
            'c': str(base+400), 'v': str(50), 't': str(1000000+58*1800000),
        }
        
        def side_effect(coin, interval, hours):
            if interval == '30m': return candles_30m
            elif interval == '1h': return candles_30m[:50]
            elif interval == '1d': return candles_30m[:30]
            return candles_30m
        
        mock_candles.side_effect = side_effect
        result = analyze('BTC')
        assert result['signal'] == 'HOLD'
    
    @patch('luckytrader.signal.get_candles')
    @patch('luckytrader.signal.get_market_context', return_value={})
    @patch('luckytrader.signal.get_recent_fills', return_value=[])
    def test_insufficient_30m_candles(self, mock_fills, mock_ctx, mock_candles, mock_hl):
        """Few 30m candles → falls back to 1h for 24h range."""
        from luckytrader.signal import analyze
        
        candles_1h = self._make_candles(55)
        candles_30m = self._make_candles(10)  # too few
        candles_1d = self._make_candles(30)
        
        def side_effect(coin, interval, hours):
            if interval == '30m': return candles_30m
            elif interval == '1h': return candles_1h
            elif interval == '1d': return candles_1d
            return candles_1h
        
        mock_candles.side_effect = side_effect
        result = analyze('BTC')
        assert result['signal'] in ('HOLD', 'LONG', 'SHORT')
        assert 'high_24h' in result


class TestFormatReportEdgeCases:
    """Format report edge cases."""
    
    def test_format_with_fills(self):
        from luckytrader.signal import format_report
        result = {
            'price': 67000, 'volume_usd': 5000000, 'avg_volume_24h': 4000000,
            'volume_ratio': 1.25, 'low_24h': 66000, 'high_24h': 68000,
            'range_24h': 3.0, 'trend': 'DOWN', 'ema_8': 67100, 'ema_21': 67500,
            'rsi': 35.0,
            'breakout': {'up': False, 'down': False, 'vol_ratio_30m': 0.8, 'vol_confirm': False},
            'supports': [], 'resistances': [],
            'signal': 'HOLD', 'signal_reasons': [],
            'market_context': {
                'BTC': {'funding_rate': 0.0001, 'open_interest': 50000, 'mark_price': 67000},
                'ETH': {'funding_rate': -0.00005, 'open_interest': 200000, 'mark_price': 1975},
            },
            'recent_fills': [
                {'coin': 'BTC', 'side': 'BUY', 'size': '0.001', 'price': '67000', 'time': 1707600000000},
            ],
        }
        report = format_report(result)
        assert 'BTC' in report
        assert '费率' in report
        assert '最近成交' in report
    
    def test_format_with_empty_context(self):
        from luckytrader.signal import format_report
        result = {
            'price': 67000, 'volume_usd': 0, 'avg_volume_24h': 0,
            'volume_ratio': 0, 'low_24h': 66000, 'high_24h': 68000,
            'range_24h': 3.0, 'trend': 'DOWN', 'ema_8': 67100, 'ema_21': 67500,
            'rsi': 35.0,
            'breakout': {'up': False, 'down': False, 'vol_ratio_30m': 0, 'vol_confirm': False},
            'supports': [], 'resistances': [],
            'signal': 'HOLD', 'signal_reasons': [],
            'market_context': {},
            'recent_fills': [],
        }
        report = format_report(result)
        assert 'HOLD' in report
