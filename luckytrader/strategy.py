"""
Strategy Module — 策略核心逻辑（纯计算，零 IO）
================================================
这是唯一的策略实现。回测和实盘都 import 这个模块。

设计原则：
- 只接受数据（K线列表），不调 API
- 只返回结果（信号、退出原因），不下单
- 所有参数从 config 传入，不硬编码

调用方：
- signal.py (实盘): 拿 API 数据 → 调 strategy → 下单
- backtest_engine.py: 拿历史数据 → 调 strategy → 模拟
"""
from typing import Optional, Tuple, Dict, List
from luckytrader.regime import compute_de, get_regime_params


# ─── 技术指标（纯计算）───────────────────────────────────

def ema(data: List[float], period: int) -> List[float]:
    """Exponential Moving Average"""
    if not data:
        return []
    result = [data[0]]
    k = 2 / (period + 1)
    for i in range(1, len(data)):
        result.append(data[i] * k + result[-1] * (1 - k))
    return result


def rsi(data: List[float], period: int = 14) -> List[float]:
    """Relative Strength Index"""
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


# ─── 信号检测 ───────────────────────────────────────────

def detect_signal(candles_30m: list, candles_4h: list,
                  idx: int, cfg, coin_cfg=None) -> Optional[str]:
    """检测交易信号（纯计算，不调 API）
    
    逻辑：
    1. 用 range_bars 窗口计算区间高低点（排除当前 K 线自身）
    2. 检查上一根已收盘 K 线是否突破区间
    3. 放量确认（vol_threshold）
    4. 4h 趋势方向过滤（顺势交易）
    
    Args:
        candles_30m: 30m K 线列表（dict, keys: h/l/c/v/t）
        candles_4h:  4h K 线列表
        idx:         当前 30m K 线索引（检查 idx-1 是否突破）
        cfg:         配置对象（strategy.range_bars, lookback_bars, vol_threshold）
        coin_cfg:    可选的 CoinConfig，提供 per-coin 覆盖参数
    
    Returns:
        'LONG' | 'SHORT' | None
    """
    if coin_cfg is not None:
        range_bars = coin_cfg.range_bars
        lookback_bars = coin_cfg.lookback_bars
        vol_threshold = coin_cfg.vol_threshold
    else:
        range_bars = cfg.strategy.range_bars
        lookback_bars = cfg.strategy.lookback_bars
        vol_threshold = cfg.strategy.vol_threshold

    # 数据充足性检查
    if idx < range_bars + 2 or idx < lookback_bars + 2:
        return None

    # 区间：用 idx-2 往前 range_bars 根（排除突破 K 线自身，和 signal.py analyze() 一致）
    range_slice = candles_30m[idx - range_bars - 1:idx - 1]
    if len(range_slice) < range_bars:
        return None

    high_range = max(float(c['h']) for c in range_slice)
    low_range = min(float(c['l']) for c in range_slice)

    # 突破判定：用上一根已收盘 K 线（idx-1）的 high/low
    bar = candles_30m[idx - 1]
    bar_high = float(bar['h'])
    bar_low = float(bar['l'])

    breakout_up = bar_high > high_range
    breakout_down = bar_low < low_range

    if not breakout_up and not breakout_down:
        return None

    # 放量确认（lookback 窗口均值）
    vol_start = max(0, idx - 1 - lookback_bars)
    vol_slice = candles_30m[vol_start:idx - 1]
    bar_vol = float(bar['v']) * float(bar['c'])
    avg_vol = sum(float(c['v']) * float(c['c']) for c in vol_slice) / len(vol_slice) if vol_slice else 1
    vol_ratio = bar_vol / avg_vol if avg_vol > 0 else 0

    if vol_ratio < vol_threshold:
        return None

    # 4h 趋势方向过滤（支持 per-coin trend EMA period）
    if coin_cfg is not None:
        trend_ema_period = getattr(coin_cfg, 'trend_ema_period', 0)
    else:
        trend_ema_period = getattr(cfg.strategy, 'trend_ema_period', 0) if hasattr(cfg, 'strategy') else 0
    if not isinstance(trend_ema_period, (int, float)):
        trend_ema_period = 0
    trend_ema_period = int(trend_ema_period)
    bar_ts = int(candles_30m[idx].get('t', candles_30m[idx].get('T', 0)))
    trend_4h = get_trend_4h(candles_4h, bar_ts, trend_ema_period)

    if breakout_up:
        return None if trend_4h == 'DOWN' else 'LONG'
    elif breakout_down:
        return None if trend_4h == 'UP' else 'SHORT'

    return None


def get_trend_4h(candles_4h: list, bar_time_ms: int, trend_ema_period: int = 0) -> str:
    """计算某个时间点的 4h 趋势方向
    
    Args:
        candles_4h: 4h K 线列表
        bar_time_ms: 当前时间戳（毫秒）
        trend_ema_period: 趋势 EMA 周期。0 = 使用经典 EMA8/EMA21 交叉（BTC 默认）。
                          >0 = 使用单 EMA：价格 > EMA = UP（ETH 用 96）。
    
    Returns:
        'UP' | 'DOWN' | 'UNKNOWN'
    """
    min_bars = max(21, trend_ema_period) if trend_ema_period > 0 else 21
    if not candles_4h or len(candles_4h) < min_bars:
        return 'UNKNOWN'

    # 找到 <= bar_time 的最近 4h K 线
    i4h = len(candles_4h) - 1
    while i4h >= 0 and int(candles_4h[i4h]['t']) > bar_time_ms:
        i4h -= 1

    if i4h < min_bars - 1:
        return 'UNKNOWN'

    closes_4h = [float(c['c']) for c in candles_4h[:i4h + 1]]

    if trend_ema_period > 0:
        # Single EMA: price above EMA = UP
        trend_ema = ema(closes_4h, trend_ema_period)
        return 'UP' if closes_4h[-1] > trend_ema[-1] else 'DOWN'
    else:
        # Classic EMA8/EMA21 crossover (BTC default)
        ema8_4h = ema(closes_4h, 8)
        ema21_4h = ema(closes_4h, 21)
        return 'UP' if ema8_4h[-1] > ema21_4h[-1] else 'DOWN'


def get_range_levels(candles_30m: list, idx: int, range_bars: int) -> Optional[Tuple[float, float]]:
    """计算区间高低点（供 signal.py analyze() 展示用）
    
    Returns:
        (high_range, low_range) or None
    """
    if idx < range_bars + 2:
        return None
    range_slice = candles_30m[idx - range_bars - 1:idx - 1]
    if len(range_slice) < range_bars:
        return None
    high_range = max(float(c['h']) for c in range_slice)
    low_range = min(float(c['l']) for c in range_slice)
    return (high_range, low_range)


def get_vol_ratio(candles_30m: list, idx: int, lookback_bars: int) -> Tuple[float, float, float]:
    """计算放量比率（供 signal.py analyze() 展示用）
    
    Returns:
        (bar_vol, avg_vol, vol_ratio)
    """
    bar = candles_30m[idx - 1]
    bar_vol = float(bar['v']) * float(bar['c'])
    vol_start = max(0, idx - 1 - lookback_bars)
    vol_slice = candles_30m[vol_start:idx - 1]
    avg_vol = sum(float(c['v']) * float(c['c']) for c in vol_slice) / len(vol_slice) if vol_slice else 1
    vol_ratio = bar_vol / avg_vol if avg_vol > 0 else 0
    return (bar_vol, avg_vol, vol_ratio)


# ─── 退出判断 ───────────────────────────────────────────

def check_exit(direction: str, entry_price: float, current_price: float,
               bars_held: int, tp_pct: float, sl_pct: float,
               max_hold_bars: int) -> Optional[Tuple[str, float]]:
    """检查是否应该退出持仓（纯计算）
    
    Args:
        direction: 'LONG' | 'SHORT'
        entry_price: 开仓价
        current_price: 当前价
        bars_held: 已持仓 K 线数
        tp_pct: 止盈百分比（小数，如 0.02）
        sl_pct: 止损百分比（小数，如 0.05）
        max_hold_bars: 最大持仓 K 线数
    
    Returns:
        (reason, pnl_pct) 或 None
        reason: 'TP' | 'SL' | 'TIMEOUT'
        pnl_pct: 盈亏百分比（小数）
    """
    if direction == 'LONG':
        pnl = (current_price - entry_price) / entry_price
    else:
        pnl = (entry_price - current_price) / entry_price

    if pnl >= tp_pct:
        return ('TP', pnl)
    if pnl <= -sl_pct:
        return ('SL', pnl)
    if bars_held >= max_hold_bars:
        return ('TIMEOUT', pnl)
    return None


# ─── 动态 Regime 重估 ──────────────────────────────────

def should_tighten_tp(old_tp_pct: float, new_de: Optional[float],
                      cfg) -> Optional[float]:
    """判断是否需要收紧 TP（纯计算）
    
    规则（和实盘 reeval_regime_tp 完全一致）：
    - DE=None → 不调整（API 失败安全）
    - 只收紧不放松
    - SL 不动
    
    Args:
        old_tp_pct: 当前 TP（小数）
        new_de: 新计算的 DE 值，可能为 None
        cfg: 配置对象
    
    Returns:
        新的 tp_pct（小数）如果需要收紧，否则 None
    """
    if new_de is None:
        return None

    new_params = get_regime_params(new_de, cfg)
    new_tp_pct = new_params['tp_pct']

    # 只收紧不放松
    if new_tp_pct >= old_tp_pct:
        return None

    return new_tp_pct


def compute_tp_price(entry_price: float, tp_pct: float, is_long: bool) -> float:
    """计算 TP 价格"""
    if is_long:
        return round(entry_price * (1 + tp_pct))
    else:
        return round(entry_price * (1 - tp_pct))


def compute_sl_price(entry_price: float, sl_pct: float, is_long: bool) -> float:
    """计算 SL 价格"""
    if is_long:
        return round(entry_price * (1 - sl_pct))
    else:
        return round(entry_price * (1 + sl_pct))


def compute_pnl_pct(direction: str, entry_price: float, exit_price: float) -> float:
    """计算盈亏百分比"""
    if direction == 'LONG':
        return (exit_price - entry_price) / entry_price
    else:
        return (entry_price - exit_price) / entry_price
