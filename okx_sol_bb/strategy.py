"""
BB Mean-Reversion Strategy — signal detection for SOL on OKX.
==============================================================
Imports indicators from core. No duplicate implementations.

Entry (mean-reversion — opposite of ETH BB breakout):
  LONG:  prev_close < lower BB AND curr_close >= lower BB (bounce off lower)
  SHORT: prev_close > upper BB AND curr_close <= upper BB (bounce off upper)

Validated: OKX 1m-resolution backtest, 760 days, OOS +14.8%, MDD 10%
Parameters: BB(14, 3.0), TP=2%, SL=5%, MaxHold=48h
"""
import sys
from pathlib import Path
from typing import Optional, List

_core_dir = str(Path(__file__).parent.parent)
if _core_dir not in sys.path:
    sys.path.insert(0, _core_dir)

from core.indicators import bollinger_bands


def detect_signal(closes: List[float], bb_period: int, bb_mult: float,
                  idx: int) -> Optional[str]:
    """Detect BB mean-reversion signal at bar `idx`.

    BB from PRIOR bars [idx-period : idx] — no look-ahead.
    Signal: price crosses back inside BB after being outside.

    No trend filter — mean-reversion works in both directions.

    Args:
        closes: List of close prices
        bb_period: BB lookback period (in bars)
        bb_mult: BB standard deviation multiplier
        idx: Current bar index (signal checks idx-1 → idx cross)

    Returns:
        'LONG' | 'SHORT' | None
    """
    if idx < bb_period + 1:
        return None

    bb = bollinger_bands(closes, bb_period, bb_mult, idx)
    if bb is None:
        return None

    _, upper, lower = bb

    prev_close = closes[idx - 1]
    curr_close = closes[idx]

    # Mean-reversion: price was outside BB, now crossing back inside
    if prev_close < lower and curr_close >= lower:
        return 'LONG'
    elif prev_close > upper and curr_close <= upper:
        return 'SHORT'

    return None


def get_bb_levels(closes: List[float], bb_period: int, bb_mult: float,
                  idx: int):
    """Get current BB levels for display/charting."""
    return bollinger_bands(closes, bb_period, bb_mult, idx)
