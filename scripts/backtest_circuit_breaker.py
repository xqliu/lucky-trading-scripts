#!/usr/bin/env python3
"""
Circuit Breaker 回测：持仓期间检测到 1 分钟异动 → 立刻平仓 vs 等 SL 被打

逻辑：
- 对每笔历史交易的持仓区间，拉 1 分钟 K 线
- 逐分钟计算涨跌幅
- 如果单根 1m K 线涨跌 > threshold 且方向与持仓相反 → 以最差价格平仓
  - 持空遇涨 → 平仓价 = high（最差买入价）
  - 持多遇跌 → 平仓价 = low（最差卖出价）
- 对比：circuit breaker 平仓 PnL vs 实际平仓 PnL

数据源：Hyperliquid 1m candles API
"""

import requests
import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# HL API
HL_API = "https://api.hyperliquid.xyz/info"
WALLET = "0xa24e75a6f48c99ec9abda7b9dba5c7c9663f918b"


def get_1m_candles(coin: str, start_ms: int, end_ms: int) -> list:
    """Get 1-minute candles from Hyperliquid."""
    # Single request — HL returns up to ~5000 candles per request (~3.5 days of 1m data)
    # For longer periods, paginate by day
    all_candles = []
    cursor = start_ms
    while cursor < end_ms:
        chunk_end = min(cursor + 86400000 * 3, end_ms)  # 3 days max per request
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
            break
        all_candles.extend(candles)
        last_t = candles[-1]["t"]
        if last_t <= cursor or last_t >= end_ms:
            break
        cursor = last_t + 60000
        time.sleep(0.3)
    return all_candles


def get_fills() -> list:
    """Get all fills from Hyperliquid."""
    resp = requests.post(HL_API, json={
        "type": "userFills",
        "user": WALLET
    })
    return resp.json()


def reconstruct_trades(fills: list) -> list:
    """Reconstruct trades from fills: pair Open → Close fills."""
    trades = []
    # Sort by time
    fills_sorted = sorted(fills, key=lambda f: f["time"])
    
    # Track open positions per coin
    open_positions = {}
    
    for fill in fills_sorted:
        coin = fill["coin"]
        direction = fill.get("dir", "")
        
        if "Open" in direction:
            open_positions[coin] = {
                "coin": coin,
                "direction": "LONG" if fill["side"] == "B" else "SHORT",
                "entry_price": float(fill["px"]),
                "size": float(fill["sz"]),
                "entry_time": fill["time"],
            }
        elif "Close" in direction and coin in open_positions:
            pos = open_positions.pop(coin)
            pos["exit_price"] = float(fill["px"])
            pos["exit_time"] = fill["time"]
            pos["closed_pnl"] = float(fill.get("closedPnl", 0))
            
            # Calculate PnL %
            if pos["direction"] == "LONG":
                pos["pnl_pct"] = (pos["exit_price"] - pos["entry_price"]) / pos["entry_price"] * 100
            else:
                pos["pnl_pct"] = (pos["entry_price"] - pos["exit_price"]) / pos["entry_price"] * 100
            
            trades.append(pos)
    
    return trades


def simulate_circuit_breaker(trade: dict, candles: list, threshold_pct: float) -> dict:
    """
    Simulate circuit breaker on a single trade.
    
    Returns dict with:
    - triggered: bool
    - trigger_time: ms timestamp when triggered
    - trigger_price: worst-case exit price
    - cb_pnl_pct: PnL if circuit breaker fires
    - original_pnl_pct: actual PnL
    - saved: cb_pnl_pct - original_pnl_pct (positive = CB saved money)
    """
    result = {
        "triggered": False,
        "trigger_time": None,
        "trigger_price": None,
        "cb_pnl_pct": None,
        "original_pnl_pct": trade["pnl_pct"],
        "saved": None,
    }
    
    entry_time = trade["entry_time"]
    exit_time = trade["exit_time"]
    direction = trade["direction"]
    entry_price = trade["entry_price"]
    
    # Filter candles to holding period (round entry down to minute, exit up)
    entry_minute = (entry_time // 60000) * 60000
    exit_minute = ((exit_time // 60000) + 1) * 60000
    holding_candles = [c for c in candles if entry_minute <= c["t"] <= exit_minute]
    
    prev_close = None
    for candle in holding_candles:
        c_open = float(candle["o"])
        c_close = float(candle["c"])
        c_high = float(candle["h"])
        c_low = float(candle["l"])
        
        if prev_close is None:
            prev_close = c_close
            continue
        
        # 1-minute change
        change_pct = (c_close - prev_close) / prev_close * 100
        
        # Check if adverse move exceeds threshold
        adverse = False
        if direction == "SHORT" and change_pct > threshold_pct:
            # Short position, price surged up → exit at high (worst case)
            trigger_price = c_high
            adverse = True
        elif direction == "LONG" and change_pct < -threshold_pct:
            # Long position, price dumped → exit at low (worst case)
            trigger_price = c_low
            adverse = True
        
        if adverse:
            # Calculate PnL at trigger price
            if direction == "LONG":
                cb_pnl_pct = (trigger_price - entry_price) / entry_price * 100
            else:
                cb_pnl_pct = (entry_price - trigger_price) / entry_price * 100
            
            result["triggered"] = True
            result["trigger_time"] = candle["t"]
            result["trigger_price"] = trigger_price
            result["cb_pnl_pct"] = cb_pnl_pct
            result["saved"] = cb_pnl_pct - trade["pnl_pct"]
            break
        
        prev_close = c_close
    
    return result


def main():
    print("=" * 70)
    print("Circuit Breaker 回测")
    print("=" * 70)
    
    # Get all fills and reconstruct trades
    print("\n📥 拉取历史成交记录...")
    fills = get_fills()
    trades = reconstruct_trades(fills)
    print(f"共 {len(trades)} 笔完整交易")
    
    # Filter to BTC/ETH/SOL
    target_coins = {"BTC", "ETH", "SOL"}
    trades = [t for t in trades if t["coin"] in target_coins]
    print(f"BTC/ETH/SOL 交易: {len(trades)} 笔")
    
    # Thresholds to test
    thresholds = [0.5, 0.8, 1.0, 1.5, 2.0]
    
    for threshold in thresholds:
        print(f"\n{'=' * 70}")
        print(f"阈值: 1 分钟涨跌 > {threshold}%")
        print(f"{'=' * 70}")
        
        triggered_count = 0
        saved_total = 0
        hurt_total = 0
        saved_trades = []
        hurt_trades = []
        
        for i, trade in enumerate(trades):
            coin = trade["coin"]
            entry_ms = trade["entry_time"]
            exit_ms = trade["exit_time"]
            
            # Skip very short trades (< 2 min)
            if exit_ms - entry_ms < 120000:
                continue
            
            print(f"\r  处理 {i+1}/{len(trades)} {coin} {trade['direction']}...", end="", flush=True)
            
            # Get 1m candles for holding period
            candles = get_1m_candles(coin, entry_ms, exit_ms)
            if len(candles) < 2:
                continue
            
            result = simulate_circuit_breaker(trade, candles, threshold)
            
            if result["triggered"]:
                triggered_count += 1
                saved = result["saved"]
                
                ts_entry = datetime.fromtimestamp(entry_ms/1000, tz=timezone.utc)
                ts_trigger = datetime.fromtimestamp(result["trigger_time"]/1000, tz=timezone.utc)
                
                entry = {
                    "coin": coin,
                    "direction": trade["direction"],
                    "entry_time": ts_entry.strftime("%m-%d %H:%M"),
                    "trigger_time": ts_trigger.strftime("%m-%d %H:%M"),
                    "original_pnl": trade["pnl_pct"],
                    "cb_pnl": result["cb_pnl_pct"],
                    "saved": saved,
                    "minutes_held": (result["trigger_time"] - entry_ms) / 60000,
                }
                
                if saved > 0:
                    saved_total += saved
                    saved_trades.append(entry)
                else:
                    hurt_total += saved
                    hurt_trades.append(entry)
        
        print()  # newline after progress
        
        total_triggered = len(saved_trades) + len(hurt_trades)
        print(f"\n📊 结果:")
        print(f"  总交易数: {len(trades)}")
        print(f"  触发次数: {total_triggered} ({total_triggered/len(trades)*100:.1f}%)")
        print(f"  有益触发（CB 减少亏损）: {len(saved_trades)} 笔, 累计省 {saved_total:+.2f}%")
        print(f"  有害触发（CB 提前跑错了）: {len(hurt_trades)} 笔, 累计多亏 {hurt_total:+.2f}%")
        print(f"  净效果: {saved_total + hurt_total:+.2f}%")
        
        if saved_trades:
            print(f"\n  ✅ 有益触发详情:")
            for t in saved_trades:
                print(f"    {t['coin']} {t['direction']} {t['entry_time']} → "
                      f"CB@{t['trigger_time']} ({t['minutes_held']:.0f}min) "
                      f"原PnL={t['original_pnl']:+.2f}% → CB PnL={t['cb_pnl']:+.2f}% "
                      f"(省 {t['saved']:+.2f}%)")
        
        if hurt_trades:
            print(f"\n  ❌ 有害触发详情:")
            for t in hurt_trades:
                print(f"    {t['coin']} {t['direction']} {t['entry_time']} → "
                      f"CB@{t['trigger_time']} ({t['minutes_held']:.0f}min) "
                      f"原PnL={t['original_pnl']:+.2f}% → CB PnL={t['cb_pnl']:+.2f}% "
                      f"(多亏 {t['saved']:+.2f}%)")


if __name__ == "__main__":
    main()
