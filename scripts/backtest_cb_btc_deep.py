#!/usr/bin/env python3
"""
BTC Circuit Breaker 深度回测 — 365 天 1m K 线，细粒度阈值扫描

1. 下载 365 天 BTC 1m K 线（如本地没有）
2. 阈值 0.3-1.0 步长 0.1 扫描
3. 分季度/月度拆分看稳定性
"""

import requests
import time
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict

OKX_BASE = "https://www.okx.com"
DATA_DIR = Path(__file__).parent.parent / "data" / "candles_1m"
SL_PCT = 3.5
TP_PCT = 2.0
MAX_HOLD_MINS = 3600  # 60h


def download_btc_365d():
    """Download 365 days of BTC 1m candles."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    outfile = DATA_DIR / "BTC_365d_1m.json"
    
    if outfile.exists():
        data = json.loads(outfile.read_text())
        expected = 365 * 1440
        if len(data) > expected * 0.9:
            print(f"Already have {len(data)} candles ({len(data)/1440:.0f} days), skipping download")
            return data
    
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    target_start = now_ms - 365 * 86400000
    
    all_candles = []
    cursor = now_ms
    batch = 0
    last_print = time.time()
    
    print("Downloading 365 days of BTC 1m candles from OKX...")
    while True:
        batch += 1
        try:
            resp = requests.get(f"{OKX_BASE}/api/v5/market/history-candles", params={
                "instId": "BTC-USDT-SWAP", "bar": "1m", "after": str(cursor), "limit": "100",
            }, timeout=10)
            data = resp.json()
        except Exception as e:
            print(f"  Error batch {batch}: {e}")
            time.sleep(2)
            continue
        
        if data["code"] != "0" or not data["data"]:
            break
        
        candles = data["data"]
        all_candles.extend(candles)
        oldest_ts = int(candles[-1][0])
        
        if oldest_ts <= target_start:
            break
        cursor = oldest_ts
        
        if time.time() - last_print > 15:
            fetched_days = (now_ms - oldest_ts) / 86400000
            print(f"  batch {batch}, {len(all_candles)} candles, {fetched_days:.0f}/365 days")
            last_print = time.time()
            sys.stdout.flush()
        
        time.sleep(0.1)
    
    # Reverse, dedup, filter, sort
    all_candles.reverse()
    seen = set()
    deduped = []
    for c in all_candles:
        if c[0] not in seen:
            seen.add(c[0])
            deduped.append(c)
    deduped = [c for c in deduped if int(c[0]) >= target_start]
    deduped.sort(key=lambda c: int(c[0]))
    
    outfile.write_text(json.dumps(deduped))
    print(f"Saved {len(deduped)} candles ({len(deduped)/1440:.0f} days) to {outfile}")
    sys.stdout.flush()
    return deduped


def load_candles(raw_data: list) -> list:
    return [{"t": int(c[0]), "o": float(c[1]), "h": float(c[2]), "l": float(c[3]), "c": float(c[4])} for c in raw_data]


def simulate_trade(candles, start_idx, entry_price, direction):
    sl = entry_price * (1 + SL_PCT/100) if direction == "SHORT" else entry_price * (1 - SL_PCT/100)
    tp = entry_price * (1 - TP_PCT/100) if direction == "SHORT" else entry_price * (1 + TP_PCT/100)
    
    end = min(start_idx + MAX_HOLD_MINS, len(candles))
    for j in range(start_idx, end):
        c = candles[j]
        if direction == "SHORT":
            if c["h"] >= sl:
                return {"exit": "SL", "pnl": -SL_PCT, "mins": j - start_idx}
            if c["l"] <= tp:
                return {"exit": "TP", "pnl": TP_PCT, "mins": j - start_idx}
        else:
            if c["l"] <= sl:
                return {"exit": "SL", "pnl": -SL_PCT, "mins": j - start_idx}
            if c["h"] >= tp:
                return {"exit": "TP", "pnl": TP_PCT, "mins": j - start_idx}
    
    last_c = candles[min(end - 1, len(candles) - 1)]["c"]
    if direction == "SHORT":
        pnl = (entry_price - last_c) / entry_price * 100
    else:
        pnl = (last_c - entry_price) / entry_price * 100
    return {"exit": "TIMEOUT", "pnl": pnl, "mins": end - start_idx}


def run_backtest(candles, threshold, entry_offset=30, slippage_pct=0.05, cooldown_mins=5):
    """Run CB backtest with realistic assumptions.
    
    Args:
        candles: 1m candle data
        threshold: CB trigger threshold (%)
        entry_offset: minutes before spike that position was entered
        slippage_pct: execution slippage added to CB exit (% adverse)
        cooldown_mins: after a spike fires, ignore next N minutes (dedup overlapping spikes)
    """
    results = []
    last_spike_time = 0  # timestamp of last triggered spike (for cooldown)
    
    for i in range(entry_offset + 1, len(candles) - MAX_HOLD_MINS):
        prev_close = candles[i-1]["c"]
        curr = candles[i]

        # Cooldown: skip if too close to last spike
        if curr["t"] - last_spike_time < cooldown_mins * 60000:
            continue

        # Real-time trigger: check if HIGH or LOW breached threshold vs prev close
        up_change = (curr["h"] - prev_close) / prev_close * 100
        down_change = (curr["l"] - prev_close) / prev_close * 100

        is_up_spike = up_change >= threshold
        is_down_spike = down_change <= -threshold

        if not is_up_spike and not is_down_spike:
            continue

        # If both directions spike in same candle, use the larger move
        if is_up_spike and is_down_spike:
            is_up = abs(up_change) >= abs(down_change)
        else:
            is_up = is_up_spike

        direction = "SHORT" if is_up else "LONG"
        entry_price = candles[i - entry_offset]["c"]

        # CB exit price = threshold breach point + slippage (adverse direction)
        if is_up:
            cb_exit = prev_close * (1 + threshold / 100) * (1 + slippage_pct / 100)
        else:
            cb_exit = prev_close * (1 - threshold / 100) * (1 - slippage_pct / 100)

        if direction == "SHORT":
            cb_pnl = (entry_price - cb_exit) / entry_price * 100
        else:
            cb_pnl = (cb_exit - entry_price) / entry_price * 100
        
        # Natural outcome
        natural = simulate_trade(candles, i + 1, entry_price, direction)
        
        # Month for grouping
        ts = datetime.fromtimestamp(curr["t"] / 1000, tz=timezone.utc)
        month = ts.strftime("%Y-%m")
        
        results.append({
            "time": curr["t"],
            "month": month,
            "spike": up_change if is_up else down_change,
            "cb_pnl": cb_pnl,
            "natural_exit": natural["exit"],
            "natural_pnl": natural["pnl"],
            "delta": cb_pnl - natural["pnl"],
        })
        
        last_spike_time = curr["t"]  # set cooldown
    
    return results


def print_summary(results, label=""):
    """Print summary stats for a set of results."""
    if not results:
        print(f"  {label}: 0 spikes")
        return
    n = len(results)
    wins = sum(1 for r in results if r["delta"] > 0)
    avg_delta = sum(r["delta"] for r in results) / n
    avg_cb = sum(r["cb_pnl"] for r in results) / n
    avg_nat = sum(r["natural_pnl"] for r in results) / n
    marker = "✅" if avg_delta > 0 else "❌"
    return n, wins/n*100, avg_delta, avg_cb, avg_nat, marker


def main():
    raw = download_btc_365d()
    candles = load_candles(raw)
    days = len(candles) / 1440
    print(f"\nLoaded {len(candles)} candles ({days:.0f} days)")
    print(f"Range: {datetime.fromtimestamp(candles[0]['t']/1000, tz=timezone.utc):%Y-%m-%d} → "
          f"{datetime.fromtimestamp(candles[-1]['t']/1000, tz=timezone.utc):%Y-%m-%d}")
    print(f"\nSL={SL_PCT}% TP={TP_PCT}% 超时={MAX_HOLD_MINS//60}h")
    sys.stdout.flush()
    
    # ═══════════════════════════════════════════════════════════════
    # PART 1: Baseline with realistic defaults
    # slippage=0.05%, cooldown=5min, entry_offset=30
    # ═══════════════════════════════════════════════════════════════
    print(f"\n{'='*80}")
    print("PART 1: Realistic baseline (slippage=0.05%, cooldown=5min, offset=30)")
    print(f"{'='*80}")
    
    thresholds = [round(t/10, 1) for t in range(3, 11)]
    
    print(f"\n{'Thr':>5} | {'Spikes':>6} | {'CB Win%':>7} | {'Avg Δ':>8} | {'CB PnL':>8} | {'Nat PnL':>8}")
    print("-" * 60)
    
    baseline_results = {}
    for threshold in thresholds:
        results = run_backtest(candles, threshold, entry_offset=30, slippage_pct=0.05, cooldown_mins=5)
        baseline_results[threshold] = results
        if not results:
            print(f"{threshold:5.1f} | {'0':>6} | {'N/A':>7}")
            continue
        n, wr, avg_d, avg_cb, avg_nat, m = print_summary(results)
        print(f"{threshold:5.1f} | {n:>6} | {wr:>6.1f}% | {avg_d:>+7.3f}% | {avg_cb:>+7.3f}% | {avg_nat:>+7.3f}% {m}")
    sys.stdout.flush()
    
    # ═══════════════════════════════════════════════════════════════
    # PART 2: entry_offset sensitivity (threshold=0.5%)
    # How much does assumed entry time change results?
    # ═══════════════════════════════════════════════════════════════
    print(f"\n{'='*80}")
    print("PART 2: Entry offset sensitivity (threshold=0.5%, slippage=0.05%, cooldown=5min)")
    print(f"{'='*80}")
    
    offsets = [5, 10, 15, 20, 30, 45, 60, 90, 120]
    print(f"\n{'Offset':>8} | {'Spikes':>6} | {'CB Win%':>7} | {'Avg Δ':>8} | {'CB PnL':>8} | {'Nat PnL':>8}")
    print("-" * 65)
    
    offset_deltas = []
    for offset in offsets:
        results = run_backtest(candles, 0.5, entry_offset=offset, slippage_pct=0.05, cooldown_mins=5)
        if not results:
            continue
        n, wr, avg_d, avg_cb, avg_nat, m = print_summary(results)
        offset_deltas.append(avg_d)
        print(f"{offset:>6}m | {n:>6} | {wr:>6.1f}% | {avg_d:>+7.3f}% | {avg_cb:>+7.3f}% | {avg_nat:>+7.3f}% {m}")
    sys.stdout.flush()
    
    if offset_deltas:
        print(f"\n  Range of Avg Δ across offsets: {min(offset_deltas):+.3f}% to {max(offset_deltas):+.3f}%")
        print(f"  Mean: {sum(offset_deltas)/len(offset_deltas):+.3f}%")
    
    # ═══════════════════════════════════════════════════════════════
    # PART 3: Slippage sensitivity (threshold=0.5%, offset=30)
    # ═══════════════════════════════════════════════════════════════
    print(f"\n{'='*80}")
    print("PART 3: Slippage sensitivity (threshold=0.5%, offset=30, cooldown=5min)")
    print(f"{'='*80}")
    
    slippages = [0.0, 0.02, 0.05, 0.08, 0.10, 0.15, 0.20]
    print(f"\n{'Slip':>6} | {'Spikes':>6} | {'CB Win%':>7} | {'Avg Δ':>8} | {'CB PnL':>8} | {'Nat PnL':>8}")
    print("-" * 60)
    
    for slip in slippages:
        results = run_backtest(candles, 0.5, entry_offset=30, slippage_pct=slip, cooldown_mins=5)
        if not results:
            continue
        n, wr, avg_d, avg_cb, avg_nat, m = print_summary(results)
        print(f"{slip:>5.2f}% | {n:>6} | {wr:>6.1f}% | {avg_d:>+7.3f}% | {avg_cb:>+7.3f}% | {avg_nat:>+7.3f}% {m}")
    sys.stdout.flush()
    
    # ═══════════════════════════════════════════════════════════════
    # PART 4: Cooldown sensitivity (threshold=0.5%, offset=30)
    # ═══════════════════════════════════════════════════════════════
    print(f"\n{'='*80}")
    print("PART 4: Cooldown sensitivity (threshold=0.5%, offset=30, slippage=0.05%)")
    print(f"{'='*80}")
    
    cooldowns = [0, 1, 2, 3, 5, 10, 15, 30, 60]
    print(f"\n{'Cool':>6} | {'Spikes':>6} | {'CB Win%':>7} | {'Avg Δ':>8}")
    print("-" * 40)
    
    for cd in cooldowns:
        results = run_backtest(candles, 0.5, entry_offset=30, slippage_pct=0.05, cooldown_mins=cd)
        if not results:
            continue
        n, wr, avg_d, _, _, m = print_summary(results)
        print(f"{cd:>4}m | {n:>6} | {wr:>6.1f}% | {avg_d:>+7.3f}% {m}")
    sys.stdout.flush()
    
    # ═══════════════════════════════════════════════════════════════
    # PART 5: Monthly breakdown (best config from above)
    # ═══════════════════════════════════════════════════════════════
    print(f"\n{'='*80}")
    print("PART 5: Monthly breakdown — 0.5% threshold, realistic params")
    print(f"{'='*80}")
    
    results = baseline_results.get(0.5, [])
    months = defaultdict(list)
    for r in results:
        months[r["month"]].append(r)
    
    print(f"\n{'Month':>7} | {'Spikes':>6} | {'CB Win%':>7} | {'Avg Δ':>8}")
    print("-" * 40)
    
    pos_months = 0
    for month in sorted(months.keys()):
        mr = months[month]
        n = len(mr)
        wins = sum(1 for r in mr if r["delta"] > 0)
        avg_delta = sum(r["delta"] for r in mr) / n
        marker = "✅" if avg_delta > 0 else "❌"
        if avg_delta > 0:
            pos_months += 1
        print(f"{month:>7} | {n:>6} | {wins/n*100:>6.1f}% | {avg_delta:>+7.3f}% {marker}")
    
    total_months = len(months)
    print(f"\nPositive months: {pos_months}/{total_months}")
    
    # ═══════════════════════════════════════════════════════════════
    # VERDICT
    # ═══════════════════════════════════════════════════════════════
    print(f"\n{'='*80}")
    print("VERDICT")
    print(f"{'='*80}")
    
    best = baseline_results.get(0.5, [])
    if best:
        n, wr, avg_d, avg_cb, avg_nat, m = print_summary(best)
        annual_triggers = n / (days / 365)
        annual_dollar = annual_triggers * avg_d / 100 * 210  # assuming $210 account
        print(f"\n  Best threshold: 0.5%")
        print(f"  Avg delta per spike: {avg_d:+.3f}%")
        print(f"  Win rate: {wr:.1f}%")
        print(f"  Spikes/year (est): {annual_triggers:.0f}")
        print(f"  Annual $ impact (on $210): ${annual_dollar:+.1f}")
        print(f"  Annual % impact: {annual_triggers * avg_d:+.1f}%")
        print(f"  Positive months: {pos_months}/{total_months}")
        if offset_deltas:
            print(f"  Entry offset sensitivity: {min(offset_deltas):+.3f}% to {max(offset_deltas):+.3f}%")


if __name__ == "__main__":
    main()
