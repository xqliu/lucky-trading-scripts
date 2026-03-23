#!/usr/bin/env python3
"""
CB 回测 v4 — 真实信号入场

不用假设 entry_offset。流程：
1. 下载 365d 的 30m + 4h + 1m K 线
2. 在历史 30m 上逐根跑 detect_signal → 得到真实入场点
3. 每笔持仓期间，检查 1m 是否触发 CB
4. 对比 CB 退出 vs 自然 SL/TP/timeout

这是唯一正确的回测方法。
"""

import json
import sys
import time
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict

# Import strategy components
sys.path.insert(0, str(Path(__file__).parent.parent))
from luckytrader.strategy import detect_signal, get_trend_4h
from luckytrader.config import get_config, get_coin_config, TradingConfig

DATA_DIR = Path(__file__).parent.parent / "data" / "candles_1m"
OKX_BASE = "https://www.okx.com"

# Trading params
SL_PCT = 3.5
TP_PCT = 2.0
TIMEOUT_MINS = 3600  # 60h


def download_candles(inst_id, bar, days, label):
    """Download candles from OKX."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    outfile = DATA_DIR / f"{label}.json"
    
    if outfile.exists():
        data = json.loads(outfile.read_text())
        if len(data) > 100:
            print(f"  {label}: {len(data)} candles cached")
            return data
    
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    target_start = now_ms - days * 86400000
    
    all_candles = []
    cursor = now_ms
    
    print(f"  Downloading {label}...")
    while True:
        try:
            resp = requests.get(f"{OKX_BASE}/api/v5/market/history-candles", params={
                "instId": inst_id, "bar": bar, "after": str(cursor), "limit": "100",
            }, timeout=10)
            data = resp.json()
        except Exception as e:
            print(f"    Error: {e}")
            time.sleep(2)
            continue
        
        if data["code"] != "0" or not data["data"]:
            break
        
        batch = data["data"]
        all_candles.extend(batch)
        cursor = int(batch[-1][0])
        
        if cursor <= target_start:
            break
        time.sleep(0.15)
    
    # Dedupe and sort
    seen = set()
    deduped = []
    for c in all_candles:
        if c[0] not in seen:
            seen.add(c[0])
            deduped.append(c)
    deduped = [c for c in deduped if int(c[0]) >= target_start]
    deduped.sort(key=lambda c: int(c[0]))
    
    outfile.write_text(json.dumps(deduped))
    print(f"  {label}: saved {len(deduped)} candles")
    return deduped


def parse_candles(raw):
    """Convert raw OKX candles to dict format expected by strategy."""
    return [{"t": int(c[0]), "T": int(c[0]), "o": c[1], "h": c[2], "l": c[3], 
             "c": c[4], "v": c[5]} for c in raw]


def simulate_natural_trade(candles_1m, start_idx, entry_price, direction):
    """Simulate natural SL/TP/timeout outcome using 1m candles."""
    sl = entry_price * (1 + SL_PCT/100) if direction == "SHORT" else entry_price * (1 - SL_PCT/100)
    tp = entry_price * (1 - TP_PCT/100) if direction == "SHORT" else entry_price * (1 + TP_PCT/100)
    
    end = min(start_idx + TIMEOUT_MINS, len(candles_1m))
    for j in range(start_idx, end):
        c = candles_1m[j]
        h, l = float(c["h"]), float(c["l"])
        if direction == "SHORT":
            if h >= sl:
                return {"exit": "SL", "pnl_pct": -SL_PCT, "mins": j - start_idx, "exit_price": sl}
            if l <= tp:
                return {"exit": "TP", "pnl_pct": TP_PCT, "mins": j - start_idx, "exit_price": tp}
        else:
            if l <= sl:
                return {"exit": "SL", "pnl_pct": -SL_PCT, "mins": j - start_idx, "exit_price": sl}
            if h >= tp:
                return {"exit": "TP", "pnl_pct": TP_PCT, "mins": j - start_idx, "exit_price": tp}
    
    # Timeout
    last_c = float(candles_1m[min(end - 1, len(candles_1m) - 1)]["c"])
    if direction == "SHORT":
        pnl = (entry_price - last_c) / entry_price * 100
    else:
        pnl = (last_c - entry_price) / entry_price * 100
    return {"exit": "TIMEOUT", "pnl_pct": pnl, "mins": end - start_idx, "exit_price": last_c}


def check_cb_during_trade(candles_1m, start_idx, end_idx, prev_1m_close, entry_price, direction, threshold, cooldown_mins=5):
    """Check if CB would trigger during a trade's lifetime.
    
    Returns first CB trigger info or None.
    """
    last_candle_close = prev_1m_close  # reference: the 1m close just before trade opens
    
    for j in range(start_idx, min(end_idx, len(candles_1m))):
        c = candles_1m[j]
        h, l = float(c["h"]), float(c["l"])
        
        if last_candle_close is None or last_candle_close == 0:
            last_candle_close = float(c["c"])
            continue
        
        # Check if high or low breaches threshold vs last reference close
        up_pct = (h - last_candle_close) / last_candle_close * 100
        down_pct = (l - last_candle_close) / last_candle_close * 100
        
        triggered = False
        is_up = False
        
        if up_pct >= threshold:
            is_up = True
            triggered = True
        if down_pct <= -threshold:
            if not triggered or abs(down_pct) > abs(up_pct):
                is_up = False
            triggered = True
        
        if triggered:
            # Is this adverse to our position?
            is_adverse = (
                (direction == "SHORT" and is_up) or
                (direction == "LONG" and not is_up)
            )
            
            if is_adverse:
                # CB exit at threshold breach point
                if is_up:
                    cb_exit_price = last_candle_close * (1 + threshold / 100)
                else:
                    cb_exit_price = last_candle_close * (1 - threshold / 100)
                
                if direction == "SHORT":
                    cb_pnl = (entry_price - cb_exit_price) / entry_price * 100
                else:
                    cb_pnl = (cb_exit_price - entry_price) / entry_price * 100
                
                return {
                    "triggered": True,
                    "minute": j - start_idx,
                    "cb_exit_price": cb_exit_price,
                    "cb_pnl_pct": cb_pnl,
                    "spike_pct": up_pct if is_up else down_pct,
                }
        
        # Update reference (end of each 1m candle)
        last_candle_close = float(c["c"])
    
    return None


def find_1m_index(candles_1m, target_ts):
    """Binary search for the 1m candle at or after target_ts."""
    lo, hi = 0, len(candles_1m) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if int(candles_1m[mid]["t"]) < target_ts:
            lo = mid + 1
        else:
            hi = mid
    return lo


def main():
    print("=" * 70)
    print("CB Realistic Backtest — Signal-based entries")
    print("=" * 70)
    
    COIN = "BTC"
    DAYS = 365
    INST = "BTC-USDT-SWAP"
    
    # Step 1: Download data
    print("\nStep 1: Download historical data")
    raw_1m = download_candles(INST, "1m", DAYS, f"{COIN}_365d_1m")
    raw_30m = download_candles(INST, "30m", DAYS, f"{COIN}_365d_30m")
    raw_4h = download_candles(INST, "4H", DAYS, f"{COIN}_365d_4h")
    
    candles_1m = parse_candles(raw_1m)
    candles_30m = parse_candles(raw_30m)
    candles_4h = parse_candles(raw_4h)
    
    print(f"\n  1m: {len(candles_1m)} candles")
    print(f"  30m: {len(candles_30m)} candles")
    print(f"  4h: {len(candles_4h)} candles")
    
    # Step 2: Run signal detection on historical 30m candles
    print("\nStep 2: Generate signals from historical 30m candles")
    
    cfg = get_config()
    try:
        coin_cfg = get_coin_config(COIN)
    except:
        coin_cfg = None
    
    trades = []  # List of {entry_time, entry_price, direction, signal_idx}
    
    # Need enough history before starting signal detection
    min_idx = max(
        getattr(coin_cfg, 'range_bars', cfg.strategy.range_bars) + 5,
        getattr(coin_cfg, 'lookback_bars', cfg.strategy.lookback_bars) + 5,
    )
    
    in_trade = False
    trade_end_ts = 0  # Don't open new trade until previous one ends
    
    for idx in range(min_idx, len(candles_30m)):
        bar_ts = int(candles_30m[idx]["t"])
        
        # Skip if still in previous trade
        if bar_ts < trade_end_ts:
            continue
        
        signal = detect_signal(candles_30m, candles_4h, idx, cfg, coin_cfg)
        
        if signal in ("LONG", "SHORT"):
            entry_price = float(candles_30m[idx]["c"])  # Enter at close of signal candle
            entry_ts = bar_ts + 30 * 60000  # Position opens at next candle
            
            # Calculate when trade would naturally end (timeout = 60h)
            trade_end_ts = entry_ts + TIMEOUT_MINS * 60000
            
            trades.append({
                "entry_time": entry_ts,
                "entry_price": entry_price,
                "direction": signal,
                "signal_idx": idx,
            })
    
    print(f"  Generated {len(trades)} trades over {DAYS} days")
    print(f"  ({len(trades)/DAYS*30:.1f} trades/month)")
    if not trades:
        print("  No trades generated! Check signal parameters.")
        return
    
    # Step 3: For each trade, simulate natural outcome and check CB
    print("\nStep 3: Simulate trades + CB check")
    
    thresholds = [0.3, 0.4, 0.5, 0.6, 0.7, 0.8]
    
    for threshold in thresholds:
        results = []
        
        for trade in trades:
            entry_ts = trade["entry_time"]
            entry_price = trade["entry_price"]
            direction = trade["direction"]
            
            # Find 1m candle index at entry time
            entry_1m_idx = find_1m_index(candles_1m, entry_ts)
            if entry_1m_idx >= len(candles_1m) - TIMEOUT_MINS:
                continue  # Not enough 1m data for full trade
            
            # Natural outcome (SL/TP/timeout)
            natural = simulate_natural_trade(candles_1m, entry_1m_idx, entry_price, direction)
            
            # Calculate when natural trade ends
            natural_end_idx = entry_1m_idx + natural["mins"]
            
            # Prev 1m close before entry (reference for CB)
            prev_1m_close = float(candles_1m[max(0, entry_1m_idx - 1)]["c"]) if entry_1m_idx > 0 else entry_price
            
            # Check if CB triggers during trade lifetime
            cb = check_cb_during_trade(
                candles_1m, entry_1m_idx, natural_end_idx,
                prev_1m_close, entry_price, direction, threshold
            )
            
            ts = datetime.fromtimestamp(entry_ts / 1000, tz=timezone.utc)
            
            if cb:
                delta = cb["cb_pnl_pct"] - natural["pnl_pct"]
                results.append({
                    "time": entry_ts,
                    "month": ts.strftime("%Y-%m"),
                    "direction": direction,
                    "entry_price": entry_price,
                    "cb_triggered": True,
                    "cb_minute": cb["minute"],
                    "cb_pnl": cb["cb_pnl_pct"],
                    "natural_exit": natural["exit"],
                    "natural_pnl": natural["pnl_pct"],
                    "delta": delta,
                    "spike_pct": cb["spike_pct"],
                })
            else:
                results.append({
                    "time": entry_ts,
                    "month": ts.strftime("%Y-%m"),
                    "direction": direction,
                    "entry_price": entry_price,
                    "cb_triggered": False,
                    "cb_pnl": natural["pnl_pct"],  # Same as natural (no CB)
                    "natural_exit": natural["exit"],
                    "natural_pnl": natural["pnl_pct"],
                    "delta": 0,
                })
        
        # Stats
        total = len(results)
        cb_triggered = [r for r in results if r["cb_triggered"]]
        n_cb = len(cb_triggered)
        
        if n_cb == 0:
            print(f"\n  Threshold {threshold}%: {total} trades, 0 CB triggers")
            continue
        
        wins = sum(1 for r in cb_triggered if r["delta"] > 0)
        avg_delta = sum(r["delta"] for r in cb_triggered) / n_cb
        total_delta = sum(r["delta"] for r in results)  # Include non-triggered (delta=0)
        
        # Annual dollar impact
        # Each trade uses ~$45 notional (0.0006 BTC × $70k)
        # PnL per trade = delta% × notional
        account = 210
        ann_dollar = total_delta / 100 * account  # Total delta already accounts for all trades
        
        # Natural outcome distribution for CB-triggered trades
        nat_sl = sum(1 for r in cb_triggered if r["natural_exit"] == "SL")
        nat_tp = sum(1 for r in cb_triggered if r["natural_exit"] == "TP")
        nat_to = sum(1 for r in cb_triggered if r["natural_exit"] == "TIMEOUT")
        
        print(f"\n  {'='*65}")
        print(f"  Threshold: {threshold}%")
        print(f"  {'='*65}")
        print(f"  Total trades: {total}")
        print(f"  CB triggered: {n_cb}/{total} ({n_cb/total*100:.1f}%)")
        print(f"  CB win rate: {wins}/{n_cb} ({wins/n_cb*100:.1f}%)")
        print(f"  Avg delta (CB trades): {avg_delta:+.3f}%")
        print(f"  Total delta (all trades): {total_delta:+.2f}%")
        print(f"  Annual $ impact (on ${account}): ${ann_dollar:+.1f}")
        print(f"  Natural outcome of CB trades: SL={nat_sl} TP={nat_tp} TO={nat_to}")
        
        # Monthly breakdown
        months = defaultdict(list)
        for r in results:
            months[r["month"]].append(r)
        
        print(f"\n  {'Month':>7} | {'Trades':>6} | {'CB':>4} | {'CB Win':>6} | {'Δ':>8}")
        print(f"  {'-'*45}")
        pos_months = 0
        for month in sorted(months.keys()):
            mr = months[month]
            mt = len(mr)
            mcb = [r for r in mr if r["cb_triggered"]]
            mcb_n = len(mcb)
            mcb_w = sum(1 for r in mcb if r["delta"] > 0) if mcb else 0
            md = sum(r["delta"] for r in mr)
            m = "✅" if md > 0 else ("—" if md == 0 else "❌")
            if md > 0:
                pos_months += 1
            elif md == 0:
                pos_months += 0  # neutral
            print(f"  {month:>7} | {mt:>6} | {mcb_n:>4} | {mcb_w:>6} | {md:>+7.2f}% {m}")
        
        print(f"\n  Positive months: {pos_months}/{len(months)}")
    
    # Print first few trades for sanity check
    print(f"\n{'='*70}")
    print("Sample trades (first 10)")
    print(f"{'='*70}")
    for t in trades[:10]:
        ts = datetime.fromtimestamp(t["entry_time"] / 1000, tz=timezone.utc)
        print(f"  {ts:%Y-%m-%d %H:%M} | {t['direction']:>5} @ ${t['entry_price']:,.0f}")


if __name__ == "__main__":
    main()
