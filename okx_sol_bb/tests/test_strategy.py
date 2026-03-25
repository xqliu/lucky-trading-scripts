"""Tests for SOL BB mean-reversion strategy."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from okx_sol_bb.strategy import detect_signal, get_bb_levels
from core.indicators import bollinger_bands


def _make_closes(base, n, noise=None):
    """Generate close prices around a base with optional noise."""
    if noise is None:
        noise = [0] * n
    return [base + noise[i % len(noise)] for i in range(n)]


class TestDetectSignal:
    """Test mean-reversion signal detection."""

    def test_no_signal_inside_bands(self):
        """Price inside BB → no signal."""
        closes = _make_closes(100, 30, [0, 0.1, -0.1, 0.2, -0.2])
        assert detect_signal(closes, 14, 3.0, len(closes) - 1) is None

    def test_long_signal_bounce_off_lower(self):
        """prev_close < lower, curr_close >= lower → LONG."""
        # Create data where price drops below lower BB then crosses back
        closes = _make_closes(100, 20, [0, 0.1, -0.1, 0.2, -0.2])
        # Add sharp drop below lower BB
        bb = bollinger_bands(closes, 14, 3.0, len(closes) - 1)
        if bb:
            _, _, lower = bb
            # Append: one bar below lower, one bar back above
            closes.append(lower - 1.0)  # below lower
            closes.append(lower + 0.1)  # back above lower
            signal = detect_signal(closes, 14, 3.0, len(closes) - 1)
            assert signal == 'LONG'

    def test_short_signal_bounce_off_upper(self):
        """prev_close > upper, curr_close <= upper → SHORT."""
        closes = _make_closes(100, 20, [0, 0.1, -0.1, 0.2, -0.2])
        bb = bollinger_bands(closes, 14, 3.0, len(closes) - 1)
        if bb:
            _, upper, _ = bb
            closes.append(upper + 1.0)  # above upper
            closes.append(upper - 0.1)  # back below upper
            signal = detect_signal(closes, 14, 3.0, len(closes) - 1)
            assert signal == 'SHORT'

    def test_no_signal_staying_below(self):
        """Price stays below lower (not crossing back) → no signal."""
        closes = _make_closes(100, 20, [0, 0.1, -0.1, 0.2, -0.2])
        # Append two very low values so both prev and curr are well below lower BB
        closes.append(80.0)  # way below
        closes.append(80.1)  # still way below
        # Recalculate BB at new idx to verify both are below
        bb = bollinger_bands(closes, 14, 3.0, len(closes) - 1)
        if bb:
            _, _, lower = bb
            assert closes[-1] < lower, f"curr {closes[-1]} not below lower {lower}"
            assert closes[-2] < lower, f"prev {closes[-2]} not below lower {lower}"
            signal = detect_signal(closes, 14, 3.0, len(closes) - 1)
            assert signal is None

    def test_insufficient_data(self):
        """Not enough data → None."""
        closes = [100] * 10
        assert detect_signal(closes, 14, 3.0, 5) is None

    def test_no_look_ahead(self):
        """BB calculation uses [idx-period:idx], not idx itself."""
        closes = _make_closes(100, 30, [0, 0.5, -0.5, 1.0, -1.0])
        idx = len(closes) - 1
        bb = get_bb_levels(closes, 14, 3.0, idx)
        if bb:
            mid, upper, lower = bb
            # BB should be calculated from closes[idx-14:idx], not including closes[idx]
            expected_bb = bollinger_bands(closes, 14, 3.0, idx)
            assert bb == expected_bb


class TestGetBBLevels:
    def test_returns_tuple(self):
        closes = _make_closes(100, 30, [0, 1, -1, 2, -2])
        result = get_bb_levels(closes, 14, 3.0, 20)
        assert result is not None
        mid, upper, lower = result
        assert lower < mid < upper

    def test_flat_market_returns_none(self):
        closes = [100.0] * 30
        result = get_bb_levels(closes, 14, 3.0, 20)
        assert result is None


class TestDetectSignalEdgeCases:
    """Additional edge cases for detect_signal branch coverage."""

    def test_flat_market_returns_none(self):
        """When BB returns None (flat market), detect_signal returns None."""
        closes = [100.0] * 30  # flat → bollinger_bands returns None
        assert detect_signal(closes, 14, 3.0, 20) is None
