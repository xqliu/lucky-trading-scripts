#!/usr/bin/env python3
"""
Circuit Breaker 回测 v3 — 正确的对照组

核心：对每次 1m spike，比较两个平行宇宙：
  A) CB 平仓：以该分钟最差价平仓
  B) 不平仓：继续持有直到 SL/TP/超时

假设：
- 在 spike 之前，某人以 spike 前 N 根 K 线均价开了一个反方向仓位
- SL = 3.5%（固定），TP = 2%（固定），超时 = 60h
- 比较 CB PnL vs 自然结局 PnL

为了避免依赖"入场点"的假设偏差，我们模拟：
每次 spike 时，假设持仓入场价 = spike 前 30 分钟的 close（合理假设：30 分钟信号周期）
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data" / "candles_1m"

# Strategy parameters
SL_PCT = 3.5   # stop loss %
TP_PCT = 2.0   # take profit %
MAX_HOLD_MINS = 60 * 60  # 60 hours in minutes


def load_candles(coin: str) -> list:
    """Load 1m candles from local file. OKX format: [ts, o, h, l, c, vol, ...]"""
    path = DATA_DIR / f"{coin}_90d_1m.json"
    data = json.loads(path.read_text())
    # Convert to uniform format
    candles = []
    for c in data:
        candles.append({
            "t": int(c[0]),
            "o": float(c[1]),
            "h": float(c[2]),
            "l": float(c[3]),
            "c": float(c[4]),
        })
    return candles


def simulate_trade_outcome(candles: list, start_idx: int, entry_price: float, 
                           direction: str, max_mins: int = MAX_HOLD_MINS) -> dict:
    """
    Simulate a trade from start_idx forward until SL, TP, or timeout.
    
    Returns: {"exit_reason": "SL"|"TP"|"TIMEOUT", "exit_price": float, "pnl_pct": float, "duration_mins": int}
    """
    sl_price = entry_price * (1 + SL_PCT/100) if direction == "SHORT" else entry_price * (1 - SL_PCT/100)
    tp_price = entry_price * (1 - TP_PCT/100) if direction == "SHORT" else entry_price * (1 + TP_PCT/100)
    
    for j in range(start_idx, min(start_idx + max_mins, len(candles))):
        c = candles[j]
        
        if direction == "SHORT":
            # SL hit if high >= sl_price
            if c["h"] >= sl_price:
                pnl = (entry_price - sl_price) / entry_price * 100
                return {"exit_reason": "SL", "exit_price": sl_price, "pnl_pct": pnl, 
                        "duration_mins": j - start_idx}
            # TP hit if low <= tp_price
            if c["l"] <= tp_price:
                pnl = (entry_price - tp_price) / entry_price * 100
                return {"exit_reason": "TP", "exit_price": tp_price, "pnl_pct": pnl,
                        "duration_mins": j - start_idx}
        else:  # LONG
            # SL hit if low <= sl_price
            if c["l"] <= sl_price:
                pnl = (sl_price - entry_price) / entry_price * 100
                return {"exit_reason": "SL", "exit_price": sl_price, "pnl_pct": pnl,
                        "duration_mins": j - start_idx}
            # TP hit if high >= tp_price
            if c["h"] >= tp_price:
                pnl = (tp_price - entry_price) / entry_price * 100
                return {"exit_reason": "TP", "exit_price": tp_price, "pnl_pct": pnl,
                        "duration_mins": j - start_idx}
    
    # Timeout: exit at last available close
    last_idx = min(start_idx + max_mins - 1, len(candles) - 1)
    exit_price = candles[last_idx]["c"]
    if direction == "SHORT":
        pnl = (entry_price - exit_price) / entry_price * 100
    else:
        pnl = (exit_price - entry_price) / entry_price * 100
    return {"exit_reason": "TIMEOUT", "exit_price": exit_price, "pnl_pct": pnl,
            "duration_mins": last_idx - start_idx}


def main():
    print("=" * 70)
    print("Circuit Breaker 回测 v3 — CB 平仓 vs 自然结局")
    print(f"策略参数: SL={SL_PCT}% TP={TP_PCT}% 超时={MAX_HOLD_MINS//60}h")
    print("=" * 70)
    
    coins = ["BTC", "ETH", "SOL"]
    thresholds = [0.3, 0.5, 0.8, 1.0, 1.5]
    
    # Load all data
    all_data = {}
    for coin in coins:
        print(f"Loading {coin}...", end=" ")
        candles = load_candles(coin)
        all_data[coin] = candles
        days = len(candles) / 1440
        print(f"{len(candles)} candles ({days:.1f} days)")
    
    for threshold in thresholds:
        print(f"\n{'=' * 70}")
        print(f"阈值: 1 分钟涨跌 > {threshold}%")
        print(f"{'=' * 70}")
        
        all_comparisons = []
        
        for coin in coins:
            candles = all_data[coin]
            comparisons = []
            
            # Entry offset: assume entered 30 candles (30 min) before spike
            entry_offset = 30
            
            for i in range(entry_offset + 1, len(candles) - MAX_HOLD_MINS):
                prev_close = candles[i-1]["c"]
                curr = candles[i]
                change_pct = (curr["c"] - prev_close) / prev_close * 100
                
                if abs(change_pct) < threshold:
                    continue
                
                is_up_spike = change_pct > 0
                
                # Assume someone holds opposite direction
                # Up spike → they're SHORT; Down spike → they're LONG
                direction = "SHORT" if is_up_spike else "LONG"
                entry_price = candles[i - entry_offset]["c"]
                
                # Universe A: CB fires, exit at worst price of this candle
                if is_up_spike:
                    cb_exit = curr["h"]  # SHORT exits at high
                    cb_pnl = (entry_price - cb_exit) / entry_price * 100
                else:
                    cb_exit = curr["l"]  # LONG exits at low
                    cb_pnl = (cb_exit - entry_price) / entry_price * 100
                
                # Universe B: No CB, trade continues from current candle
                natural = simulate_trade_outcome(candles, i + 1, entry_price, direction)
                
                # Compare
                delta = cb_pnl - natural["pnl_pct"]  # positive = CB was better
                
                comparisons.append({
                    "time": curr["t"],
                    "coin": coin,
                    "spike_pct": change_pct,
                    "direction": direction,
                    "entry_price": entry_price,
                    "cb_exit": cb_exit,
                    "cb_pnl": cb_pnl,
                    "natural_exit": natural["exit_reason"],
                    "natural_pnl": natural["pnl_pct"],
                    "natural_duration": natural["duration_mins"],
                    "delta": delta,
                })
            
            all_comparisons.extend(comparisons)
            
            if comparisons:
                cb_wins = [c for c in comparisons if c["delta"] > 0]
                cb_loses = [c for c in comparisons if c["delta"] <= 0]
                n = len(comparisons)
                
                # Natural outcome breakdown
                natural_sl = sum(1 for c in comparisons if c["natural_exit"] == "SL")
                natural_tp = sum(1 for c in comparisons if c["natural_exit"] == "TP")
                natural_to = sum(1 for c in comparisons if c["natural_exit"] == "TIMEOUT")
                
                avg_cb_pnl = sum(c["cb_pnl"] for c in comparisons) / n
                avg_natural_pnl = sum(c["natural_pnl"] for c in comparisons) / n
                avg_delta = sum(c["delta"] for c in comparisons) / n
                
                print(f"\n  {coin}: {n} spikes")
                print(f"    如果不 CB 的自然结局: SL={natural_sl} TP={natural_tp} 超时={natural_to}")
                print(f"    CB 平均 PnL: {avg_cb_pnl:+.3f}%")
                print(f"    自然平均 PnL: {avg_natural_pnl:+.3f}%")
                print(f"    CB 更优: {len(cb_wins)}/{n} ({len(cb_wins)/n*100:.0f}%)")
                print(f"    平均差异(正=CB好): {avg_delta:+.3f}%")
                
                # Show some interesting cases
                # CB was bad (missed TP)
                missed_tp = [c for c in comparisons if c["natural_exit"] == "TP" and c["delta"] < 0]
                if missed_tp:
                    print(f"    ⚠️ CB 导致错过止盈: {len(missed_tp)} 次")
                    for m in missed_tp[:3]:
                        ts = datetime.fromtimestamp(m["time"]/1000, tz=timezone.utc)
                        print(f"      {ts:%m-%d %H:%M} spike {m['spike_pct']:+.2f}% "
                              f"CB={m['cb_pnl']:+.2f}% 但自然结局=TP {m['natural_pnl']:+.2f}% "
                              f"(少赚 {abs(m['delta']):.2f}%)")
                
                # CB saved from SL
                saved_sl = [c for c in comparisons if c["natural_exit"] == "SL" and c["delta"] > 0]
                if saved_sl:
                    print(f"    ✅ CB 避免止损: {len(saved_sl)} 次")
                    for s in saved_sl[:3]:
                        ts = datetime.fromtimestamp(s["time"]/1000, tz=timezone.utc)
                        print(f"      {ts:%m-%d %H:%M} spike {s['spike_pct']:+.2f}% "
                              f"CB={s['cb_pnl']:+.2f}% vs SL={s['natural_pnl']:+.2f}% "
                              f"(省 {s['delta']:.2f}%)")
        
        # Overall summary
        if all_comparisons:
            n = len(all_comparisons)
            cb_wins = sum(1 for c in all_comparisons if c["delta"] > 0)
            avg_delta = sum(c["delta"] for c in all_comparisons) / n
            total_delta = sum(c["delta"] for c in all_comparisons)
            
            natural_sl = sum(1 for c in all_comparisons if c["natural_exit"] == "SL")
            natural_tp = sum(1 for c in all_comparisons if c["natural_exit"] == "TP")
            
            print(f"\n  📊 综合 ({n} spikes, 90天 BTC+ETH+SOL):")
            print(f"    自然结局: SL {natural_sl} | TP {natural_tp} | 超时 {n-natural_sl-natural_tp}")
            print(f"    CB 胜率: {cb_wins}/{n} ({cb_wins/n*100:.1f}%)")
            print(f"    平均每次差异: {avg_delta:+.4f}%")
            print(f"    总计差异: {total_delta:+.3f}%")
            if avg_delta > 0:
                print(f"    ✅ CB 划算")
            else:
                print(f"    ❌ CB 不划算（平均每次少赚 {abs(avg_delta):.4f}%）")


if __name__ == "__main__":
    main()
