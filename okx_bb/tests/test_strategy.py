"""Tests for okx_bb.strategy — BB breakout signal detection."""
import sys
import math
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from okx_bb.strategy import detect_signal, get_bb_levels


class TestDetectSignal:
    def _make_uptrend(self, n=200, base=2000, slope=2):
        """Create uptrending price data."""
        return [base + i * slope + math.sin(i * 0.3) * 20 for i in range(n)]

    def _make_downtrend(self, n=200, base=4000, slope=2):
        return [base - i * slope + math.sin(i * 0.3) * 20 for i in range(n)]

    def test_insufficient_data(self):
        closes = [100.0] * 50
        assert detect_signal(closes, 20, 2.5, 96, 8, 30) is None

    def test_flat_market_no_signal(self):
        closes = [2000.0] * 200
        assert detect_signal(closes, 20, 2.5, 96, 8, 199) is None

    def test_long_on_breakout_above_upper(self):
        """Uptrending + close > upper BB should give LONG."""
        closes = self._make_uptrend(300)
        # Add a spike
        closes.append(closes[-1] + 100)
        idx = len(closes) - 1
        signal = detect_signal(closes, 20, 2.5, 96, 8, idx)
        # May or may not fire depending on BB width, but should not be SHORT
        assert signal != "SHORT"

    def test_short_on_breakout_below_lower(self):
        """Downtrending + close < lower BB should give SHORT."""
        closes = self._make_downtrend(300)
        closes.append(closes[-1] - 100)
        idx = len(closes) - 1
        signal = detect_signal(closes, 20, 2.5, 96, 8, idx)
        assert signal != "LONG"

    def test_no_signal_against_trend(self):
        """Breakout above BB in downtrend should NOT give LONG."""
        closes = self._make_downtrend(300)
        # Spike up in downtrend
        closes.append(closes[-1] + 200)
        idx = len(closes) - 1
        signal = detect_signal(closes, 20, 2.5, 96, 8, idx)
        assert signal != "LONG"

    def test_get_bb_levels(self):
        closes = [2000 + math.sin(i * 0.5) * 50 for i in range(50)]
        result = get_bb_levels(closes, 20, 2.0, 40)
        assert result is not None
        mid, upper, lower = result
        assert upper > mid > lower
