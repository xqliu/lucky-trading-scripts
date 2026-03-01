"""Tests for backtest.py single-position enforcement (in_trade_until logic)"""
import pytest
from unittest.mock import patch, MagicMock
from luckytrader.backtest import run_backtest, simulate_trade, FEE_ROUND_TRIP_PCT


class TestSimulateTrade:
    def test_fee_deducted_on_tp(self):
        """TP exit should deduct round-trip fee"""
        # Price goes up immediately
        highs = [100, 200, 200]
        lows = [100, 100, 100]
        closes = [100, 150, 150]
        result = simulate_trade('LONG', 100, 0, highs, lows, closes, 0.05, 0.03, 10)
        # TP = 3%, fee = ~0.0864%
        assert result['reason'] == 'TP'
        expected = 3.0 - FEE_ROUND_TRIP_PCT * 100
        assert abs(result['pnl_pct'] - expected) < 0.01

    def test_fee_deducted_on_sl(self):
        """SL exit should deduct round-trip fee"""
        highs = [100, 100, 100]
        lows = [100, 90, 90]
        closes = [100, 95, 95]
        result = simulate_trade('LONG', 100, 0, highs, lows, closes, 0.05, 0.10, 10)
        assert result['reason'] == 'STOP'
        expected = -5.0 - FEE_ROUND_TRIP_PCT * 100
        assert abs(result['pnl_pct'] - expected) < 0.01


class TestSinglePosition:
    """Verify run_backtest enforces single position (no overlapping trades)"""

    def _make_candles(self, n, base_price=67000):
        """Make flat candles"""
        return [{
            'o': str(base_price), 'h': str(base_price + 10),
            'l': str(base_price - 10), 'c': str(base_price),
            'v': '100', 't': 1000000 + i * 1800000,
        } for i in range(n)]

    @patch('luckytrader.backtest.detect_signal')
    def test_no_new_signal_during_trade(self, mock_signal):
        """Should not open new position while in_trade_until > current index"""
        candles_30m = self._make_candles(50)
        candles_4h = self._make_candles(10)

        # Signal on bar 5, then another on bar 7 (should be skipped)
        def side_effect(c30, c4h, idx, cfg, coin_cfg):
            if idx == 5:
                return 'LONG'
            if idx == 7:
                return 'LONG'  # should be ignored
            return None
        mock_signal.side_effect = side_effect

        trades = run_backtest(candles_30m, candles_4h, 0.04, 0.07, 10)
        # Only 1 trade should exist (bar 7 signal skipped because still in position)
        assert len(trades) <= 1

    @patch('luckytrader.backtest.detect_signal')
    def test_can_open_after_previous_closes(self, mock_signal):
        """After a trade closes (timeout), should be able to open new one"""
        candles_30m = self._make_candles(200)
        candles_4h = self._make_candles(50)

        # Signal on bar 55 (trade holds max_hold=3 bars → timeout at bar 58/59),
        # then bar 70 (well after first trade closes)
        def side_effect(c30, c4h, idx, cfg, coin_cfg):
            if idx == 55:
                return 'LONG'
            if idx == 70:
                return 'LONG'
            return None
        mock_signal.side_effect = side_effect

        trades = run_backtest(candles_30m, candles_4h, 0.04, 0.07, 3)
        assert len(trades) == 2
