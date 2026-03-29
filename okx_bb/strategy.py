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
                  min_bb_width: float = 0.0) -> Optional[str]:
    """Detect BB breakout signal at bar `idx`.

    BB from PRIOR bars [idx-period : idx] — no look-ahead.
    Trend: EMA direction over last `trend_lookback` bars.
    min_bb_width: minimum BB bandwidth (upper-lower)/middle to accept signal.
                  0.0 = no filter (backward compatible).
    """
    min_bars = max(bb_period + 1, trend_period + trend_lookback + 1)
    if idx < min_bars:
        return None

    bb = bollinger_bands(closes, bb_period, bb_mult, idx)
    if bb is None:
        return None

    mid, upper, lower = bb
    c = closes[idx]

    # BB width filter: skip signals when BB is too narrow (choppy market)
    if min_bb_width > 0 and mid > 0:
        bb_width = (upper - lower) / mid
        if bb_width < min_bb_width:
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
