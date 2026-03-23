#!/usr/bin/env python3
"""
Download 90 days of 1m candles from OKX and save to local files.
OKX returns 100 candles per request, so 90 days = ~1300 requests per coin.
"""

import requests
import time
import json
from datetime import datetime, timezone
from pathlib import Path

OKX_BASE = "https://www.okx.com"
DATA_DIR = Path(__file__).parent.parent / "data" / "candles_1m"


def download_candles(inst_id: str, days: int = 90):
    """Download 1m candles from OKX, paginating backward."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    
    coin = inst_id.split("-")[0]
    outfile = DATA_DIR / f"{coin}_{days}d_1m.json"
    
    # Check if already downloaded recently
    if outfile.exists():
        data = json.loads(outfile.read_text())
        if len(data) > days * 1400 * 0.8:  # at least 80% complete
            print(f"  {coin}: Already have {len(data)} candles, skipping")
            return data
    
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    target_start = now_ms - days * 86400000
    
    all_candles = []
    cursor = now_ms
    batch = 0
    last_print = time.time()
    
    while True:
        batch += 1
        url = f"{OKX_BASE}/api/v5/market/history-candles"
        params = {
            "instId": inst_id,
            "bar": "1m",
            "after": str(cursor),
            "limit": "100",
        }
        
        try:
            resp = requests.get(url, params=params, timeout=10)
            data = resp.json()
        except Exception as e:
            print(f"  Error at batch {batch}: {e}, retrying...")
            time.sleep(2)
            continue
        
        if data["code"] != "0" or not data["data"]:
            print(f"  OKX returned code={data['code']}, stopping at batch {batch}")
            break
        
        candles = data["data"]
        all_candles.extend(candles)
        
        oldest_ts = int(candles[-1][0])  # OKX returns newest-first
        if oldest_ts <= target_start:
            break
        cursor = oldest_ts
        
        if time.time() - last_print > 10:
            fetched_days = (now_ms - oldest_ts) / 86400000
            print(f"  {coin}: batch {batch}, {len(all_candles)} candles, {fetched_days:.0f}/{days} days")
            last_print = time.time()
        
        time.sleep(0.12)  # ~8 req/s, well under OKX rate limit
    
    # Reverse to oldest-first and deduplicate
    all_candles.reverse()
    seen = set()
    deduped = []
    for c in all_candles:
        ts = c[0]
        if ts not in seen:
            seen.add(ts)
            deduped.append(c)
    
    # Filter to target range
    deduped = [c for c in deduped if int(c[0]) >= target_start]
    deduped.sort(key=lambda c: int(c[0]))
    
    outfile.write_text(json.dumps(deduped))
    print(f"  {coin}: Saved {len(deduped)} candles to {outfile}")
    return deduped


def main():
    coins = {
        "BTC": "BTC-USDT-SWAP",
        "ETH": "ETH-USDT-SWAP",
        "SOL": "SOL-USDT-SWAP",
    }
    
    days = 90
    print(f"Downloading {days} days of 1m candles from OKX...\n")
    
    for coin, inst_id in coins.items():
        print(f"Downloading {coin}...")
        candles = download_candles(inst_id, days)
        print()


if __name__ == "__main__":
    main()
