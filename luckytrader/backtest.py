"""
30分钟K线回测 v2 - 无防重入
每笔交易独立计算收益率，允许同时多仓
"""
from luckytrader.signal import get_candles, ema, rsi
from luckytrader.config import get_config

def simulate_trade(direction, entry, entry_idx, highs, lows, closes, stop_pct, tp_pct, max_hold):
    if direction == 'LONG':
        stop = entry * (1 - stop_pct)
        tp = entry * (1 + tp_pct)
    else:
        stop = entry * (1 + stop_pct)
        tp = entry * (1 - tp_pct)
    
    for j in range(1, min(max_hold + 1, len(closes) - entry_idx)):
        idx = entry_idx + j
        if direction == 'LONG':
            if lows[idx] <= stop:
                return {'dir': direction, 'pnl_pct': -stop_pct * 100, 'bars': j, 'reason': 'STOP'}
            if highs[idx] >= tp:
                return {'dir': direction, 'pnl_pct': tp_pct * 100, 'bars': j, 'reason': 'TP'}
        else:
            if highs[idx] >= stop:
                return {'dir': direction, 'pnl_pct': -stop_pct * 100, 'bars': j, 'reason': 'STOP'}
            if lows[idx] <= tp:
                return {'dir': direction, 'pnl_pct': tp_pct * 100, 'bars': j, 'reason': 'TP'}
    
    exit_idx = min(entry_idx + max_hold, len(closes) - 1)
    if direction == 'LONG':
        pnl = (closes[exit_idx] - entry) / entry * 100
    else:
        pnl = (entry - closes[exit_idx]) / entry * 100
    return {'dir': direction, 'pnl_pct': pnl, 'bars': exit_idx - entry_idx, 'reason': 'TIMEOUT'}

def run_strategy_b(candles, stop_pct, tp_pct, max_hold, vol_thresh=1.25):
    _cfg = get_config()
    range_bars = _cfg.strategy.range_bars
    lookback_bars = _cfg.strategy.lookback_bars

    closes = [float(c['c']) for c in candles]
    opens = [float(c['o']) for c in candles]
    highs = [float(c['h']) for c in candles]
    lows = [float(c['l']) for c in candles]
    volumes = [float(c['v']) * float(c['c']) for c in candles]

    start = max(range_bars, lookback_bars) + 1
    trades = []

    for i in range(start, len(candles) - 1):  # -1: need next candle for entry
        h_range = max(highs[i-range_bars:i])
        l_range = min(lows[i-range_bars:i])
        avg_vol = sum(volumes[i-lookback_bars:i]) / lookback_bars
        vol_ratio = volumes[i] / avg_vol if avg_vol > 0 else 0

        signal = None
        if highs[i] > h_range and vol_ratio > vol_thresh:
            signal = 'LONG'
        elif lows[i] < l_range and vol_ratio > vol_thresh:
            signal = 'SHORT'

        if signal:
            # 入场用下一根K线的开盘价（实盘中看到信号后才能下单）
            entry_price = opens[i + 1]
            trade = simulate_trade(signal, entry_price, i + 1, highs, lows, closes, stop_pct, tp_pct, max_hold)
            if trade:
                trades.append(trade)

    return trades

def print_stats(label, trades):
    if not trades:
        print(f"  {label}: 无交易")
        return
    
    wins = [t for t in trades if t['pnl_pct'] > 0]
    losses = [t for t in trades if t['pnl_pct'] <= 0]
    total_pnl = sum(t['pnl_pct'] for t in trades)
    avg_pnl = total_pnl / len(trades)
    
    stops = sum(1 for t in trades if t['reason'] == 'STOP')
    tps = sum(1 for t in trades if t['reason'] == 'TP')
    timeouts = sum(1 for t in trades if t['reason'] == 'TIMEOUT')
    wr = len(wins)/len(trades)*100
    
    print(f"  {label}: {len(trades)}笔 | 胜率{wr:.0f}% | 总{total_pnl:+.1f}% | 每笔{avg_pnl:+.2f}% | TP{tps} SL{stops} TO{timeouts}")

def main():
    candles = get_candles('BTC', '30m', 24 * 90)
    print(f"数据: {len(candles)} 根30分钟K线 (90天), 无防重入\n")
    
    print("--- 不同止盈 (SL3.5%, 24h) ---")
    for tp in [0.03, 0.04, 0.05, 0.07, 0.10]:
        trades = run_strategy_b(candles, 0.035, tp, 48)
        print_stats(f"TP{tp*100:.0f}%", trades)
    
    print("\n--- 不同止损 (TP4%, 24h) ---")
    for sl in [0.02, 0.025, 0.03, 0.035, 0.04, 0.05]:
        trades = run_strategy_b(candles, sl, 0.04, 48)
        print_stats(f"SL{sl*100:.1f}%", trades)
    
    print("\n--- 不同持仓时间 (SL3.5%, TP4%) ---")
    for hold in [12, 24, 48, 72, 96]:
        trades = run_strategy_b(candles, 0.035, 0.04, hold)
        print_stats(f"{hold*0.5:.0f}h", trades)
    
    print("\n--- 不同放量倍数 (SL3.5%, TP4%, 24h) ---")
    # need custom for vol threshold
    _cfg = get_config()
    range_bars = _cfg.strategy.range_bars
    lookback_bars = _cfg.strategy.lookback_bars
    closes = [float(c['c']) for c in candles]
    opens = [float(c['o']) for c in candles]
    highs = [float(c['h']) for c in candles]
    lows = [float(c['l']) for c in candles]
    volumes = [float(c['v']) * float(c['c']) for c in candles]
    start = max(range_bars, lookback_bars) + 1

    for vol_thresh in [1.5, 2.0, 2.5, 3.0]:
        trades = []
        for i in range(start, len(candles) - 1):
            h_range = max(highs[i-range_bars:i])
            l_range = min(lows[i-range_bars:i])
            avg_vol = sum(volumes[i-lookback_bars:i]) / lookback_bars
            vr = volumes[i] / avg_vol if avg_vol > 0 else 0

            sig = None
            if highs[i] > h_range and vr > vol_thresh: sig = 'LONG'
            elif lows[i] < l_range and vr > vol_thresh: sig = 'SHORT'

            if sig:
                entry_price = opens[i + 1]
                t = simulate_trade(sig, entry_price, i + 1, highs, lows, closes, 0.035, 0.04, 48)
                if t: trades.append(t)
        print_stats(f"Vol>{vol_thresh}x", trades)

if __name__ == '__main__':
    main()
