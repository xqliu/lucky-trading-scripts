"""
Technical Indicators — 通用技术指标（纯数学，与策略无关）
========================================================
EMA, RSI 等通用计算函数。
策略、回测、图表等模块统一从这里 import。
"""
from typing import List


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
    """Relative Strength Index."""
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
