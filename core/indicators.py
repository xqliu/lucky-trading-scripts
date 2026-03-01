"""
Technical Indicators — 通用技术指标（纯数学，与策略/交易所无关）
================================================================
所有交易系统统一从这里 import。
绝对禁止在策略文件或回测文件中重新实现这些函数。

Canonical source for: ema, rsi, bollinger_bands
"""
from typing import List, Optional, Tuple
import math


def ema(data: List[float], period: int) -> List[float]:
    """Exponential Moving Average.

    Uses standard EMA formula: EMA[i] = close[i] * k + EMA[i-1] * (1-k)
    First value is seeded with data[0].

    Note: EMA needs ~3x period bars to converge from initial seed.
    """
    if not data:
        return []
    result = [data[0]]
    k = 2 / (period + 1)
    for i in range(1, len(data)):
        result.append(data[i] * k + result[-1] * (1 - k))
    return result


def rsi(data: List[float], period: int = 14) -> List[float]:
    """Relative Strength Index.

    Returns values 0-100. First `period` values are padded with 50.
    Flat data (no movement) returns ~0 (not 50) — correct behavior.
    """
    result = [50] * min(period, len(data))
    for i in range(period, len(data)):
        gains, losses = [], []
        for j in range(i - period + 1, i + 1):
            change = data[j] - data[j - 1]
            if change > 0:
                gains.append(change)
            elif change < 0:
                losses.append(abs(change))
        avg_gain = sum(gains) / period if gains else 0
        avg_loss = sum(losses) / period if losses else 0.0001
        if avg_loss == 0:
            avg_loss = 0.0001
        rs = avg_gain / avg_loss
        result.append(100 - 100 / (1 + rs))
    return result


def bollinger_bands(closes: List[float], period: int, multiplier: float,
                    idx: int) -> Optional[Tuple[float, float, float]]:
    """Bollinger Bands from PRIOR bars [idx-period : idx] — no look-ahead.

    Returns (mid, upper, lower) or None if insufficient data or flat market.
    """
    if idx < period:
        return None

    window = closes[idx - period: idx]
    mid = sum(window) / period
    variance = sum((x - mid) ** 2 for x in window) / period
    std = math.sqrt(variance)

    if std < 1e-10:
        return None  # flat market protection

    upper = mid + multiplier * std
    lower = mid - multiplier * std
    return (mid, upper, lower)
