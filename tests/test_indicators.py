"""Tests for indicators.py — 通用技术指标的边界和正确性测试"""
import pytest
from luckytrader.indicators import ema, rsi


class TestEma:
    def test_empty_input(self):
        assert ema([], 10) == []

    def test_single_element(self):
        assert ema([42.0], 10) == [42.0]

    def test_period_one(self):
        """period=1 → k=1 → EMA = raw data"""
        data = [1.0, 2.0, 3.0, 4.0]
        result = ema(data, 1)
        assert result == data

    def test_known_values(self):
        """Verify against hand-calculated EMA(3)"""
        data = [10.0, 11.0, 12.0, 13.0, 14.0]
        k = 2 / (3 + 1)  # 0.5
        # EMA[0] = 10
        # EMA[1] = 11*0.5 + 10*0.5 = 10.5
        # EMA[2] = 12*0.5 + 10.5*0.5 = 11.25
        # EMA[3] = 13*0.5 + 11.25*0.5 = 12.125
        # EMA[4] = 14*0.5 + 12.125*0.5 = 13.0625
        result = ema(data, 3)
        assert len(result) == 5
        assert result[0] == 10.0
        assert abs(result[1] - 10.5) < 1e-10
        assert abs(result[2] - 11.25) < 1e-10
        assert abs(result[3] - 12.125) < 1e-10
        assert abs(result[4] - 13.0625) < 1e-10

    def test_constant_input(self):
        """Constant data → EMA = constant"""
        data = [50.0] * 20
        result = ema(data, 10)
        assert all(abs(v - 50.0) < 1e-10 for v in result)

    def test_length_preserved(self):
        data = list(range(100))
        result = ema([float(x) for x in data], 14)
        assert len(result) == 100


class TestRsi:
    def test_all_up(self):
        """All gains → RSI near 100"""
        data = [float(i) for i in range(30)]
        result = rsi(data, 14)
        assert result[-1] > 95

    def test_all_down(self):
        """All losses → RSI near 0"""
        data = [float(100 - i) for i in range(30)]
        result = rsi(data, 14)
        assert result[-1] < 5

    def test_flat(self):
        """No change → RSI near 0 (no gains, tiny epsilon loss denominator)"""
        data = [100.0] * 30
        result = rsi(data, 14)
        # With zero changes, avg_gain=0, avg_loss=epsilon → rs≈0 → RSI≈0
        assert result[-1] < 5

    def test_length(self):
        data = [float(i) for i in range(50)]
        result = rsi(data, 14)
        assert len(result) == 50

    def test_short_input(self):
        """Input shorter than period"""
        data = [1.0, 2.0, 3.0]
        result = rsi(data, 14)
        assert len(result) == 3  # padded with 50s
