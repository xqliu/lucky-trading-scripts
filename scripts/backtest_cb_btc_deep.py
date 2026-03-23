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


def run_backtest(candles, threshold, entry_offset=30):
    results = []
    
    for i in range(entry_offset + 1, len(candles) - MAX_HOLD_MINS):
        prev_close = candles[i-1]["c"]
        curr = candles[i]
        change = (curr["c"] - prev_close) / prev_close * 100
        
        if abs(change) < threshold:
            continue
        
        is_up = change > 0
        direction = "SHORT" if is_up else "LONG"
        entry_price = candles[i - entry_offset]["c"]
        
        # CB exit
        cb_exit = curr["h"] if is_up else curr["l"]
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
            "spike": change,
            "cb_pnl": cb_pnl,
            "natural_exit": natural["exit"],
            "natural_pnl": natural["pnl"],
            "delta": cb_pnl - natural["pnl"],
        })
    
    return results


def main():
    raw = download_btc_365d()
    candles = load_candles(raw)
    days = len(candles) / 1440
    print(f"\nLoaded {len(candles)} candles ({days:.0f} days)")
    print(f"Range: {datetime.fromtimestamp(candles[0]['t']/1000, tz=timezone.utc):%Y-%m-%d} → "
          f"{datetime.fromtimestamp(candles[-1]['t']/1000, tz=timezone.utc):%Y-%m-%d}")
    print(f"\nSL={SL_PCT}% TP={TP_PCT}% 超时={MAX_HOLD_MINS//60}h entry_offset=30min")
    sys.stdout.flush()
    
    thresholds = [round(t/10, 1) for t in range(3, 11)]  # 0.3, 0.4, ..., 1.0
    
    print(f"\n{'Thr':>5} | {'Spikes':>6} | {'CB Win%':>7} | {'Avg Δ':>8} | {'CB PnL':>8} | {'Nat PnL':>8} | {'SL':>4} | {'TP':>4} | {'TO':>4} | {'Miss TP':>7} | {'Save SL':>7}")
    print("-" * 105)
    
    all_results = {}
    
    for threshold in thresholds:
        results = run_backtest(candles, threshold)
        all_results[threshold] = results
        
        if not results:
            print(f"{threshold:5.1f} | {'0':>6} | {'N/A':>7}")
            continue
        
        n = len(results)
        wins = sum(1 for r in results if r["delta"] > 0)
        avg_delta = sum(r["delta"] for r in results) / n
        avg_cb = sum(r["cb_pnl"] for r in results) / n
        avg_nat = sum(r["natural_pnl"] for r in results) / n
        sl_count = sum(1 for r in results if r["natural_exit"] == "SL")
        tp_count = sum(1 for r in results if r["natural_exit"] == "TP")
        to_count = sum(1 for r in results if r["natural_exit"] == "TIMEOUT")
        miss_tp = sum(1 for r in results if r["natural_exit"] == "TP" and r["delta"] < 0)
        save_sl = sum(1 for r in results if r["natural_exit"] == "SL" and r["delta"] > 0)
        
        marker = "✅" if avg_delta > 0 else "❌"
        print(f"{threshold:5.1f} | {n:>6} | {wins/n*100:>6.1f}% | {avg_delta:>+7.3f}% | {avg_cb:>+7.3f}% | {avg_nat:>+7.3f}% | {sl_count:>4} | {tp_count:>4} | {to_count:>4} | {miss_tp:>7} | {save_sl:>7} {marker}")
        sys.stdout.flush()
    
    # Monthly breakdown for the most interesting threshold (0.5%)
    print(f"\n\n{'=' * 70}")
    print("BTC 0.5% 阈值 — 月度拆分")
    print(f"{'=' * 70}")
    
    target_threshold = 0.5
    results = all_results.get(target_threshold, [])
    
    months = defaultdict(list)
    for r in results:
        months[r["month"]].append(r)
    
    print(f"\n{'Month':>7} | {'Spikes':>6} | {'CB Win%':>7} | {'Avg Δ':>8} | {'SL':>3} | {'TP':>3} | {'Miss TP':>7} | {'Save SL':>7}")
    print("-" * 75)
    
    for month in sorted(months.keys()):
        mr = months[month]
        n = len(mr)
        wins = sum(1 for r in mr if r["delta"] > 0)
        avg_delta = sum(r["delta"] for r in mr) / n
        sl_count = sum(1 for r in mr if r["natural_exit"] == "SL")
        tp_count = sum(1 for r in mr if r["natural_exit"] == "TP")
        miss_tp = sum(1 for r in mr if r["natural_exit"] == "TP" and r["delta"] < 0)
        save_sl = sum(1 for r in mr if r["natural_exit"] == "SL" and r["delta"] > 0)
        marker = "✅" if avg_delta > 0 else "❌"
        print(f"{month:>7} | {n:>6} | {wins/n*100:>6.1f}% | {avg_delta:>+7.3f}% | {sl_count:>3} | {tp_count:>3} | {miss_tp:>7} | {save_sl:>7} {marker}")
    
    # Also do 0.4% monthly
    print(f"\n\n{'=' * 70}")
    print("BTC 0.4% 阈值 — 月度拆分")
    print(f"{'=' * 70}")
    
    results_04 = all_results.get(0.4, [])
    months_04 = defaultdict(list)
    for r in results_04:
        months_04[r["month"]].append(r)
    
    print(f"\n{'Month':>7} | {'Spikes':>6} | {'CB Win%':>7} | {'Avg Δ':>8} | {'SL':>3} | {'TP':>3} | {'Miss TP':>7} | {'Save SL':>7}")
    print("-" * 75)
    
    for month in sorted(months_04.keys()):
        mr = months_04[month]
        n = len(mr)
        wins = sum(1 for r in mr if r["delta"] > 0)
        avg_delta = sum(r["delta"] for r in mr) / n
        sl_count = sum(1 for r in mr if r["natural_exit"] == "SL")
        tp_count = sum(1 for r in mr if r["natural_exit"] == "TP")
        miss_tp = sum(1 for r in mr if r["natural_exit"] == "TP" and r["delta"] < 0)
        save_sl = sum(1 for r in mr if r["natural_exit"] == "SL" and r["delta"] > 0)
        marker = "✅" if avg_delta > 0 else "❌"
        print(f"{month:>7} | {n:>6} | {wins/n*100:>6.1f}% | {avg_delta:>+7.3f}% | {sl_count:>3} | {tp_count:>3} | {miss_tp:>7} | {save_sl:>7} {marker}")


if __name__ == "__main__":
    main()
