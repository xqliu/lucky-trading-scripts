"""Additional signal.py tests for branch coverage."""
import pytest
from unittest.mock import patch, MagicMock


class TestGetMarketContext:
    def test_success(self, mock_hl):
        from luckytrader.signal import get_market_context, _API_CACHE
        _API_CACHE.clear()
        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {"universe": [{"name": "BTC"}, {"name": "ETH"}, {"name": "SOL"}]},
            [
                {"funding": "0.0001", "openInterest": "5000", "markPx": "67000"},
                {"funding": "0.0002", "openInterest": "3000", "markPx": "3500"},
                {"funding": "0.0003", "openInterest": "1000", "markPx": "150"},
            ],
        ]
        with patch('requests.post', return_value=mock_resp):
            ctx = get_market_context()
        assert "BTC" in ctx
        assert "ETH" in ctx
        assert "SOL" in ctx
        assert ctx["BTC"]["mark_price"] == 67000.0

    def test_exception_returns_empty(self, mock_hl):
        from luckytrader.signal import get_market_context, _API_CACHE
        _API_CACHE.clear()
        with patch('requests.post', side_effect=Exception("timeout")):
            ctx = get_market_context()
        assert ctx == {}

    def test_caches_market_context_within_ttl(self, mock_hl):
        from luckytrader.signal import get_market_context, _API_CACHE
        _API_CACHE.clear()
        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {"universe": [{"name": "BTC"}, {"name": "ETH"}, {"name": "SOL"}]},
            [
                {"funding": "0.0001", "openInterest": "5000", "markPx": "67000"},
                {"funding": "0.0002", "openInterest": "3000", "markPx": "3500"},
                {"funding": "0.0003", "openInterest": "1000", "markPx": "150"},
            ],
        ]
        with patch('requests.post', return_value=mock_resp) as mock_post:
            first = get_market_context()
            second = get_market_context()
        assert first == second
        assert mock_post.call_count == 1


class TestGetRecentFills:
    def test_success(self, mock_hl):
        from luckytrader.signal import get_recent_fills, _API_CACHE
        _API_CACHE.clear()
        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {"coin": "BTC", "side": "B", "sz": "0.001", "px": "67000", "time": 1700000000000},
            {"coin": "ETH", "side": "A", "sz": "0.01", "px": "3500", "time": 1700000001000},
        ]
        with patch('requests.post', return_value=mock_resp):
            fills = get_recent_fills(limit=2)
        assert len(fills) == 2
        assert fills[0]["side"] == "BUY"
        assert fills[1]["side"] == "SELL"

    def test_exception_returns_empty(self, mock_hl):
        from luckytrader.signal import get_recent_fills, _API_CACHE
        _API_CACHE.clear()
        with patch('requests.post', side_effect=Exception("network")):
            fills = get_recent_fills()
        assert fills == []


class TestGetRecentTrades:
    def test_paired_close_open(self, mock_hl):
        from luckytrader.signal import get_recent_trades, _API_CACHE
        _API_CACHE.clear()
        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {"coin": "BTC", "side": "A", "sz": "0.001", "px": "68000", "time": 1700000010000,
             "dir": "Close Long", "closedPnl": "1.5"},
            {"coin": "BTC", "side": "B", "sz": "0.001", "px": "67000", "time": 1700000000000,
             "dir": "Open Long", "closedPnl": "0"},
        ]
        with patch('requests.post', return_value=mock_resp):
            trades = get_recent_trades(limit=3)
        assert len(trades) == 1
        assert trades[0]["direction"] == "LONG"
        assert trades[0]["status"] == "closed"
        assert trades[0]["pnl"] == 1.5

    def test_recent_trades_reuses_user_fills_cache(self, mock_hl):
        from luckytrader.signal import get_recent_trades, get_recent_fills, _API_CACHE
        _API_CACHE.clear()
        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {"coin": "BTC", "side": "A", "sz": "0.001", "px": "68000", "time": 1700000010000,
             "dir": "Close Long", "closedPnl": "1.5"},
            {"coin": "BTC", "side": "B", "sz": "0.001", "px": "67000", "time": 1700000000000,
             "dir": "Open Long", "closedPnl": "0"},
        ]
        with patch('requests.post', return_value=mock_resp) as mock_post:
            fills = get_recent_fills(limit=1)
            trades = get_recent_trades(limit=3)
        assert len(fills) == 1
        assert len(trades) == 1
        assert mock_post.call_count == 1

    def test_open_position(self, mock_hl):
        from luckytrader.signal import get_recent_trades, _API_CACHE
        _API_CACHE.clear()
        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {"coin": "ETH", "side": "A", "sz": "0.01", "px": "3500", "time": 1700000000000,
             "dir": "Open Short", "closedPnl": "0"},
        ]
        with patch('requests.post', return_value=mock_resp):
            trades = get_recent_trades(limit=3)
        assert len(trades) == 1
        assert trades[0]["direction"] == "SHORT"
        assert trades[0]["status"] == "open"

    def test_exception_returns_empty(self, mock_hl):
        from luckytrader.signal import get_recent_trades, _API_CACHE
        _API_CACHE.clear()
        with patch('requests.post', side_effect=Exception("api down")):
            trades = get_recent_trades()
        assert trades == []

    def test_close_without_matching_open(self, mock_hl):
        from luckytrader.signal import get_recent_trades, _API_CACHE
        _API_CACHE.clear()
        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {"coin": "BTC", "side": "A", "sz": "0.001", "px": "68000", "time": 1700000010000,
             "dir": "Close Long", "closedPnl": "2.0"},
            # No matching Open Long
        ]
        with patch('requests.post', return_value=mock_resp):
            trades = get_recent_trades(limit=3)
        assert len(trades) == 1
        assert trades[0]["open_price"] is None


class TestAnalyzeEdgeCases:
    def test_insufficient_candles(self, mock_hl):
        from luckytrader.signal import analyze
        with patch('luckytrader.signal.get_candles', return_value=[{"c": "100", "v": "1", "h": "101", "l": "99"}] * 10), \
             patch('luckytrader.signal.get_market_context', return_value={}), \
             patch('luckytrader.signal.get_recent_trades', return_value=[]):
            result = analyze("BTC")
        assert "error" in result

    def test_short_signal_branch(self, mock_hl):
        """Cover the SHORT signal branch in analyze."""
        from luckytrader.signal import analyze
        with patch('luckytrader.signal.get_candles') as mock_candles, \
             patch('luckytrader.signal.get_market_context', return_value={}), \
             patch('luckytrader.signal.get_recent_trades', return_value=[]), \
             patch('luckytrader.signal.detect_signal', return_value='SHORT'), \
             patch('luckytrader.signal.get_trend_4h', return_value='DOWN'), \
             patch('luckytrader.signal.get_range_levels'), \
             patch('luckytrader.signal.get_vol_ratio'):
            # Build sufficient candle data
            candles_1h = [{"c": str(100 + i * 0.1), "v": "1000", "h": str(101 + i * 0.1), "l": str(99 + i * 0.1)} for i in range(60)]
            candles_30m = [{"c": str(100 + i * 0.05), "v": "500", "h": str(101 + i * 0.05), "l": str(99 + i * 0.05)} for i in range(100)]
            candles_1d = [{"c": str(100 + i), "v": "10000", "h": str(105 + i), "l": str(95 + i)} for i in range(30)]
            candles_4h = [{"c": str(100 + i * 0.2), "v": "2000", "h": str(101 + i * 0.2), "l": str(99 + i * 0.2)} for i in range(40)]
            mock_candles.side_effect = [candles_1h, candles_30m, candles_1d, candles_4h]

            result = analyze("BTC")
        assert result.get("signal") == "SHORT"

    def test_signal_filtered_down_trend(self, mock_hl):
        """Cover the signal_filtered branch for LONG blocked by DOWN trend."""
        from luckytrader.signal import analyze
        with patch('luckytrader.signal.get_candles') as mock_candles, \
             patch('luckytrader.signal.get_market_context', return_value={}), \
             patch('luckytrader.signal.get_recent_trades', return_value=[]), \
             patch('luckytrader.signal.detect_signal', return_value=None), \
             patch('luckytrader.signal.get_trend_4h', return_value='DOWN'), \
             patch('luckytrader.signal.get_range_levels'), \
             patch('luckytrader.signal.get_vol_ratio'):
            candles_1h = [{"c": str(100 + i * 0.1), "v": "1000", "h": str(101 + i * 0.1), "l": str(99 + i * 0.1)} for i in range(60)]
            # Build 30m candles where latest breaks above high range with volume
            candles_30m = [{"c": str(100), "v": "500", "h": str(100.5), "l": str(99.5)} for i in range(100)]
            # Make the breakout bar break above range high
            candles_30m[-2] = {"c": "200", "v": "50000", "h": "200", "l": "100"}
            candles_1d = [{"c": str(100 + i), "v": "10000", "h": str(105 + i), "l": str(95 + i)} for i in range(30)]
            candles_4h = [{"c": str(100 + i * 0.2), "v": "2000", "h": str(101 + i * 0.2), "l": str(99 + i * 0.2)} for i in range(40)]
            mock_candles.side_effect = [candles_1h, candles_30m, candles_1d, candles_4h]

            result = analyze("BTC")
        # Signal might be HOLD with signal_filtered (or not, depends on vol_threshold)
        assert result.get("signal") == "HOLD"


class TestFormatReport:
    def test_error_report(self, mock_hl):
        from luckytrader.signal import format_report
        assert format_report({"error": "data fail"}) == "data fail"

    def test_full_report_with_short_signal(self, mock_hl):
        from luckytrader.signal import format_report
        result = {
            "coin": "BTC", "price": 67000, "volume_usd": 500000,
            "avg_volume_24h": 400000, "volume_ratio": 1.25,
            "low_24h": 65000, "high_24h": 68000, "range_24h": 4.6,
            "trend": "DOWN", "ema_8": 66800, "ema_21": 67200,
            "trend_4h": "DOWN", "rsi": 35.0,
            "breakout": {"up": False, "down": True, "vol_ratio_30m": 1.5, "vol_confirm": True},
            "supports": [(65000, 3)], "resistances": [(69000, 2)],
            "signal": "SHORT", "signal_reasons": ["跌破区间低点$65,000", "放量1.5x"],
            "suggested_stop": 69500, "suggested_tp": 63000,
            "market_context": {
                "BTC": {"funding_rate": 0.0001, "open_interest": 5000, "mark_price": 67000},
                "ETH": {"funding_rate": 0.0002, "open_interest": 3000, "mark_price": 3500},
            },
            "recent_trades": [
                {"coin": "BTC", "direction": "LONG", "open_price": 66000, "open_time": 1700000000000,
                 "close_price": 67000, "close_time": 1700001000000, "pnl": 1.5, "status": "closed"},
                {"coin": "ETH", "direction": "SHORT", "open_price": 3500, "open_time": 1700000000000,
                 "close_price": None, "close_time": None, "pnl": None, "status": "open"},
            ],
        }
        report = format_report(result)
        assert "SHORT" in report
        assert "BTC" in report
        assert "止损" in report
        assert "止盈" in report
        assert "资金费率" in report
        assert "持仓中" in report

    def test_report_with_filtered_signal(self, mock_hl):
        from luckytrader.signal import format_report
        result = {
            "coin": "BTC", "price": 67000, "volume_usd": 500000,
            "avg_volume_24h": 400000, "volume_ratio": 1.25,
            "low_24h": 65000, "high_24h": 68000, "range_24h": 4.6,
            "trend": "UP", "ema_8": 67200, "ema_21": 66800,
            "trend_4h": "DOWN", "rsi": 55.0,
            "breakout": {"up": True, "down": False, "vol_ratio_30m": 2.0, "vol_confirm": True},
            "supports": [], "resistances": [],
            "signal": "HOLD", "signal_reasons": [],
            "signal_filtered": "LONG信号被过滤（4h趋势=DOWN）",
            "market_context": {}, "recent_trades": [],
        }
        report = format_report(result)
        assert "过滤" in report
