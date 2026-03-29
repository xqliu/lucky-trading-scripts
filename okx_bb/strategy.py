"""
BB Breakout Strategy — signal detection for OKX system.
========================================================
Imports indicators from core. No duplicate implementations.

Entry:
  LONG:  close > upper BB AND trend EMA rising
  SHORT: close < lower BB AND trend EMA falling
"""
import sys
from pathlib import Path
from typing import Optional, List

# Add core to path
_core_dir = str(Path(__file__).parent.parent)
if _core_dir not in sys.path:
    sys.path.insert(0, _core_dir)

from core.indicators import ema, bollinger_bands


def detect_signal(closes: List[float], bb_period: int, bb_mult: float,
                  trend_period: int, trend_lookback: int,
                  idx: int,
                  min_bb_width: float = 0.0,
                  bb_width_kill_lo: float = 0.0,
                  bb_width_kill_hi: float = 0.0) -> Optional[str]:
    """Detect BB breakout signal at bar `idx`.

    BB from PRIOR bars [idx-period : idx] — no look-ahead.
    Trend: EMA direction over last `trend_lookback` bars.

    BB width filters (two modes, can combine):
      min_bb_width: simple threshold — skip if width < min_bb_width
      bb_width_kill_lo/hi: kill zone — skip if kill_lo <= width < kill_hi
    """
    min_bars = max(bb_period + 1, trend_period + trend_lookback + 1)
    if idx < min_bars:
        return None

    bb = bollinger_bands(closes, bb_period, bb_mult, idx)
    if bb is None:
        return None

    mid, upper, lower = bb
    c = closes[idx]

    # BB width filters
    if mid > 0:
        bb_width = (upper - lower) / mid
        # Simple threshold filter
        if min_bb_width > 0 and bb_width < min_bb_width:
            return None
        # Kill zone filter: reject signals in the "dead zone"
        if bb_width_kill_lo > 0 and bb_width_kill_hi > bb_width_kill_lo:
            if bb_width_kill_lo <= bb_width < bb_width_kill_hi:
                return None

    # Trend EMA — use 3x period for convergence
    ema_start = max(0, idx - trend_period * 3)
    ema_data = closes[ema_start:idx + 1]
    ema_vals = ema(ema_data, trend_period)

    if len(ema_vals) < trend_lookback + 1:
        return None

    trend_rising = ema_vals[-1] > ema_vals[-1 - trend_lookback]
    trend_falling = ema_vals[-1] < ema_vals[-1 - trend_lookback]

    if c > upper and trend_rising:
        return 'LONG'
    elif c < lower and trend_falling:
        return 'SHORT'

    return None


def get_bb_levels(closes: List[float], bb_period: int, bb_mult: float,
                  idx: int):
    """Get current BB levels for display/charting."""
    return bollinger_bands(closes, bb_period, bb_mult, idx)
