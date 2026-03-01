"""Tests for core.indicators — shared indicator library."""
import sys
from pathlib import Path

# Add parent dirs to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.indicators import ema, rsi, bollinger_bands


class TestEMA:
    def test_empty(self):
        assert ema([], 10) == []

    def test_single(self):
        assert ema([100.0], 10) == [100.0]

    def test_constant(self):
        result = ema([50.0] * 20, 10)
        assert all(abs(v - 50.0) < 1e-10 for v in result)

    def test_trending_up(self):
        data = [float(i) for i in range(50)]
        result = ema(data, 10)
        # EMA should lag behind in uptrend
        assert result[-1] < data[-1]
        assert result[-1] > result[-2]

    def test_known_value(self):
        data = [22.27, 22.19, 22.08, 22.17, 22.18, 22.13, 22.23, 22.43, 22.24, 22.29]
        result = ema(data, 10)
        assert len(result) == 10


class TestRSI:
    def test_flat(self):
        result = rsi([100.0] * 20, 14)
        # Flat data → avg_gain=0, RSI ≈ 0
        assert result[-1] < 5

    def test_strong_uptrend(self):
        data = [float(i) for i in range(20)]
        result = rsi(data, 14)
        assert result[-1] > 90  # All gains, no losses

    def test_strong_downtrend(self):
        data = [float(100 - i) for i in range(20)]
        result = rsi(data, 14)
        assert result[-1] < 10


class TestBollingerBands:
    def test_insufficient_data(self):
        assert bollinger_bands([1.0, 2.0], 20, 2.0, 1) is None

    def test_flat_market(self):
        # All same price → std ≈ 0 → None
        closes = [100.0] * 30
        assert bollinger_bands(closes, 20, 2.0, 25) is None

    def test_normal(self):
        import math
        closes = [100 + math.sin(i * 0.3) * 5 for i in range(50)]
        result = bollinger_bands(closes, 20, 2.0, 30)
        assert result is not None
        mid, upper, lower = result
        assert upper > mid > lower

    def test_no_lookahead(self):
        """BB at idx should not use closes[idx]."""
        closes = [100.0] * 30
        closes.append(200.0)  # spike at idx=30
        result = bollinger_bands(closes, 20, 2.0, 30)
        # Window is [10:30] — doesn't include idx=30
        assert result is None  # all 100.0 → flat → None

    def test_upper_lower_symmetric(self):
        import math
        closes = [100 + math.sin(i * 0.5) * 10 for i in range(50)]
        result = bollinger_bands(closes, 20, 2.0, 40)
        mid, upper, lower = result
        assert abs((upper - mid) - (mid - lower)) < 1e-10
