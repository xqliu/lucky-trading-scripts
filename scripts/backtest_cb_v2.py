#!/usr/bin/env python3
"""
Circuit Breaker 回测 v2 — Hyperliquid 全量 1m K 线

核心问题：当 1 分钟内出现 >X% 异动时，如果你持有反方向仓位：
- 在该分钟的最差价格平仓 vs 继续持有，哪个更好？
- "更好" = 后续 30 分钟内价格是否继续恶化

数据源：Hyperliquid candleSnapshot API（单次可拉数千根）
"""

import requests
import time
import json
from datetime import datetime, timezone, timedelta

HL_API = "https://api.hyperliquid.xyz/info"


def fetch_1m_candles_hl(coin: str, days: int = 90) -> list:
    """Fetch N days of 1m candles from Hyperliquid."""
    all_candles = []
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    target_start = now_ms - days * 86400000
    
    cursor = target_start
    batch = 0
    while cursor < now_ms:
        batch += 1
        chunk_end = min(cursor + 86400000, now_ms)  # 1 day per request
        resp = requests.post(HL_API, json={
            "type": "candleSnapshot",
            "req": {
                "coin": coin,
                "interval": "1m",
                "startTime": cursor,
                "endTime": chunk_end,
            }
        })
        candles = resp.json()
        if not candles:
            cursor = chunk_end
            time.sleep(0.1)
            continue
        
        all_candles.extend(candles)
        last_t = candles[-1]["t"]
        cursor = last_t + 60000
        
        if batch % 10 == 0:
            fetched_days = (last_t - target_start) / 86400000
            print(f"  {coin}: {batch} batches, {len(all_candles)} candles, {fetched_days:.0f}/{days} days")
        
        time.sleep(0.15)
    
    print(f"  {coin}: Total {len(all_candles)} candles ({len(all_candles)/1440:.1f} days)")
    return all_candles


def analyze_spikes(candles: list, threshold: float, coin: str) -> dict:
    """
    Find all 1m spikes > threshold, analyze CB vs hold.
    
    HL candle format: {"t":ms, "o":"px", "h":"px", "l":"px", "c":"px", "v":"vol", "n":trades}
    """
    results = {
        "threshold": threshold,
        "coin": coin,
        "total_candles": len(candles),
        "spikes": [],
        "up_spikes": 0,
        "down_spikes": 0,
        "cb_saves_count": 0,
        "cb_hurts_count": 0,
        "cb_saves_total_pct": 0,
        "cb_hurts_total_pct": 0,
    }
    
    for i in range(1, len(candles) - 60):
        prev_close = float(candles[i-1]["c"])
        curr = candles[i]
        curr_close = float(curr["c"])
        curr_high = float(curr["h"])
        curr_low = float(curr["l"])
        
        change_pct = (curr_close - prev_close) / prev_close * 100
        
        if abs(change_pct) < threshold:
            continue
        
        is_up = change_pct > 0
        if is_up:
            results["up_spikes"] += 1
        else:
            results["down_spikes"] += 1
        
        # CB exit price (worst case for opposite-direction holder)
        cb_exit = curr_high if is_up else curr_low
        
        # Worst price in next 30 mins for opposite-direction holder
        if is_up:
            worst_after_30m = max(float(candles[j]["h"]) for j in range(i, min(i+30, len(candles))))
        else:
            worst_after_30m = min(float(candles[j]["l"]) for j in range(i, min(i+30, len(candles))))
        
        # Price at +30m
        price_30m = float(candles[min(i+30, len(candles)-1)]["c"])
        
        # CB saves if price continued moving against position
        if is_up:
            cb_vs_worst = (worst_after_30m - cb_exit) / cb_exit * 100
            continuation = (price_30m - curr_close) / curr_close * 100  # positive = kept going up
        else:
            cb_vs_worst = (cb_exit - worst_after_30m) / cb_exit * 100
            continuation = (curr_close - price_30m) / curr_close * 100  # positive = kept going down
        
        spike_info = {
            "time": curr["t"],
            "change_pct": change_pct,
            "cb_exit": cb_exit,
            "worst_30m": worst_after_30m,
            "cb_vs_worst_pct": cb_vs_worst,
            "price_30m": price_30m,
            "continuation_pct": continuation,
        }
        results["spikes"].append(spike_info)
        
        if cb_vs_worst > 0:
            results["cb_saves_count"] += 1
            results["cb_saves_total_pct"] += cb_vs_worst
        else:
            results["cb_hurts_count"] += 1
            results["cb_hurts_total_pct"] += abs(cb_vs_worst)
    
    return results


def main():
    print("=" * 70)
    print("Circuit Breaker 回测 v2 — HL 全量 1m K 线")
    print("=" * 70)
    
    coins = ["BTC", "ETH", "SOL"]
    days = 90
    
    print(f"\n📥 拉取 {days} 天 1m K 线...")
    all_data = {}
    for coin in coins:
        print(f"\n拉取 {coin}...")
        candles = fetch_1m_candles_hl(coin, days=days)
        all_data[coin] = candles
    
    thresholds = [0.3, 0.5, 0.8, 1.0, 1.5, 2.0]
    
    for threshold in thresholds:
        print(f"\n{'=' * 70}")
        print(f"阈值: 1 分钟涨跌 > {threshold}%")
        print(f"{'=' * 70}")
        
        total_spikes = 0
        total_saves = 0
        total_hurts = 0
        total_save_pct = 0
        total_hurt_pct = 0
        
        for coin in coins:
            candles = all_data[coin]
            result = analyze_spikes(candles, threshold, coin)
            n_spikes = len(result["spikes"])
            total_spikes += n_spikes
            total_saves += result["cb_saves_count"]
            total_hurts += result["cb_hurts_count"]
            total_save_pct += result["cb_saves_total_pct"]
            total_hurt_pct += result["cb_hurts_total_pct"]
            
            if n_spikes > 0:
                save_rate = result["cb_saves_count"] / n_spikes * 100
                avg_save = result["cb_saves_total_pct"] / max(result["cb_saves_count"], 1)
                avg_hurt = result["cb_hurts_total_pct"] / max(result["cb_hurts_count"], 1)
                
                # Continuation rate: how often does the spike direction continue?
                cont_count = sum(1 for s in result["spikes"] if s["continuation_pct"] > 0)
                cont_rate = cont_count / n_spikes * 100
                
                print(f"\n  {coin}: {n_spikes} spikes ({result['up_spikes']}↑ {result['down_spikes']}↓)")
                print(f"    CB 有益: {result['cb_saves_count']} ({save_rate:.0f}%) avg省 {avg_save:.3f}%")
                print(f"    CB 有害: {result['cb_hurts_count']} ({100-save_rate:.0f}%) avg多亏 {avg_hurt:.3f}%")
                print(f"    30m后继续同向: {cont_rate:.0f}%")
                
                # Top 3 biggest spikes
                biggest = sorted(result["spikes"], key=lambda s: abs(s["change_pct"]), reverse=True)[:3]
                for s in biggest:
                    ts = datetime.fromtimestamp(s["time"]/1000, tz=timezone.utc)
                    d = "↑" if s["change_pct"] > 0 else "↓"
                    v = "✅" if s["cb_vs_worst_pct"] > 0 else "❌"
                    print(f"    {ts:%m-%d %H:%M} {d}{abs(s['change_pct']):.2f}% "
                          f"CB@{s['cb_exit']:.0f} worst30m@{s['worst_30m']:.0f} "
                          f"{v}{abs(s['cb_vs_worst_pct']):.3f}% "
                          f"30m后{s['continuation_pct']:+.2f}%")
        
        if total_spikes > 0:
            overall_save_rate = total_saves / total_spikes * 100
            net_effect = total_save_pct - total_hurt_pct
            avg_save = total_save_pct / max(total_saves, 1)
            avg_hurt = total_hurt_pct / max(total_hurts, 1)
            ev = net_effect / total_spikes
            print(f"\n  📊 综合 ({total_spikes} spikes across {days}d):")
            print(f"    CB 有益率: {overall_save_rate:.1f}%")
            print(f"    avg有益: +{avg_save:.3f}% | avg有害: -{avg_hurt:.3f}%")
            print(f"    净效果: {net_effect:+.3f}%")
            print(f"    每次触发期望值: {ev:+.4f}%")
            if ev > 0:
                print(f"    ✅ CB 划算：平均每次触发能省 {ev:.4f}%")
            else:
                print(f"    ❌ CB 不划算：平均每次触发多亏 {abs(ev):.4f}%")


if __name__ == "__main__":
    main()
