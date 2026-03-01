"""
30分钟K线回测 v3 — 使用 strategy.detect_signal() 单一真相源

铁律：信号生成只在 strategy.py，回测只负责模拟持仓和统计。
"""
from luckytrader.strategy import detect_signal
from luckytrader.signal import get_candles
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


def run_backtest(candles_30m, candles_4h, stop_pct, tp_pct, max_hold,
                 cfg=None, coin_cfg=None):
    """使用 strategy.detect_signal() 跑回测。

    Args:
        candles_30m: 30m K线列表 (dict with h/l/c/o/v/t keys)
        candles_4h:  4h K线列表
        stop_pct:    止损百分比 (e.g. 0.04)
        tp_pct:      止盈百分比 (e.g. 0.07)
        max_hold:    最大持仓 bars
        cfg:         config 对象 (默认用 get_config())
        coin_cfg:    可选的 per-coin 配置
    """
    if cfg is None:
        cfg = get_config()

    closes = [float(c['c']) for c in candles_30m]
    opens = [float(c['o']) for c in candles_30m]
    highs = [float(c['h']) for c in candles_30m]
    lows = [float(c['l']) for c in candles_30m]

    trades = []
    in_trade_until = 0  # 单仓制：持仓期间不开新仓

    for i in range(1, len(candles_30m) - 1):
        if i <= in_trade_until:
            continue

        signal = detect_signal(candles_30m, candles_4h, i, cfg, coin_cfg)
        if signal is None:
            continue

        # 入场用下一根K线的开盘价
        entry_price = opens[i + 1]
        trade = simulate_trade(signal, entry_price, i + 1,
                               highs, lows, closes, stop_pct, tp_pct, max_hold)
        if trade:
            trades.append(trade)
            in_trade_until = i + 1 + trade['bars']

    return trades


def print_stats(label, trades):
    if not trades:
        print(f"  {label}: 无交易")
        return

    wins = [t for t in trades if t['pnl_pct'] > 0]
    total_pnl = sum(t['pnl_pct'] for t in trades)
    avg_pnl = total_pnl / len(trades)
    stops = sum(1 for t in trades if t['reason'] == 'STOP')
    tps = sum(1 for t in trades if t['reason'] == 'TP')
    timeouts = sum(1 for t in trades if t['reason'] == 'TIMEOUT')
    wr = len(wins) / len(trades) * 100

    print(f"  {label}: {len(trades)}笔 | 胜率{wr:.0f}% | "
          f"总{total_pnl:+.1f}% | 每笔{avg_pnl:+.2f}% | "
          f"TP{tps} SL{stops} TO{timeouts}")


def main():
    cfg = get_config()
    candles_30m = get_candles('BTC', '30m', 24 * 90)
    candles_4h = get_candles('BTC', '4h', 24 * 90 // 8)
    print(f"数据: {len(candles_30m)} 根30分钟K线 (90天), 单仓制\n")
    print(f"信号源: strategy.detect_signal() (与实盘一致)\n")

    print("--- 不同止盈 (SL4%, 60h) ---")
    for tp in [0.02, 0.03, 0.04, 0.05, 0.07]:
        trades = run_backtest(candles_30m, candles_4h, 0.04, tp, 120, cfg)
        print_stats(f"TP{tp*100:.0f}%", trades)

    print("\n--- 不同止损 (TP7% trend/2% range, 60h) ---")
    for sl in [0.02, 0.03, 0.035, 0.04, 0.05]:
        trades = run_backtest(candles_30m, candles_4h, sl, 0.07, 120, cfg)
        print_stats(f"SL{sl*100:.1f}%", trades)

    print("\n--- 不同持仓时间 (SL4%, TP7%) ---")
    for hold_hours in [12, 24, 36, 48, 60]:
        hold_bars = hold_hours * 2  # 30m bars
        trades = run_backtest(candles_30m, candles_4h, 0.04, 0.07, hold_bars, cfg)
        print_stats(f"{hold_hours}h", trades)


if __name__ == '__main__':
    main()
