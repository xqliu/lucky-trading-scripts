#!/usr/bin/env python3
"""
BB Width Regime Filter Backtest

Tests hypothesis: filtering out trades when BB is narrow (choppy market)
improves overall performance.

BB Width (bandwidth) = (upper - lower) / middle
- High width = volatile/trending = good for breakout
- Low width = tight/ranging = bad for breakout (false signals)
"""
import os, sys, numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ['OKX_BB_CONFIG_DIR'] = '/home/xqianliu/.openclaw/workspace/trading/okx_bb/config'

from okx_bb.backtest import backtest_close_confirm_buffer, load_or_fetch_candles
from okx_bb.config import load_config

cfg = load_config()
candles = load_or_fetch_candles(cfg)
total_days = (candles[-1]['ts'] - candles[0]['ts']) / 86400000
print(f"Data: {len(candles)} candles ({total_days:.0f} days)\n")

# Compute BB width for each candle
closes = np.array([c['c'] for c in candles])
bb_period = cfg.strategy.bb_period

bb_widths = np.zeros(len(closes))
for i in range(bb_period, len(closes)):
    window = closes[i-bb_period+1:i+1]
    mid = np.mean(window)
    std = np.std(window, ddof=0)
    width = (2 * cfg.strategy.bb_multiplier * std) / mid
    bb_widths[i] = width

# Get all trades
trades = backtest_close_confirm_buffer(candles, cfg)
print(f"Total trades (no filter): {len(trades)}")

# Attach BB width to each trade
for t in trades:
    t.bb_width = bb_widths[t.entry_idx] if t.entry_idx < len(bb_widths) else 0

# Analyze BB width distribution
widths = [t.bb_width for t in trades]
print(f"BB Width stats: min={min(widths):.4f} median={np.median(widths):.4f} "
      f"mean={np.mean(widths):.4f} max={max(widths):.4f}")
print(f"Percentiles: 10%={np.percentile(widths,10):.4f} 25%={np.percentile(widths,25):.4f} "
      f"50%={np.percentile(widths,50):.4f} 75%={np.percentile(widths,75):.4f}")

# Win rate by BB width quartile
quartiles = np.percentile(widths, [25, 50, 75])
bins = [("Q1 (narrow)", lambda w: w <= quartiles[0]),
        ("Q2", lambda w: quartiles[0] < w <= quartiles[1]),
        ("Q3", lambda w: quartiles[1] < w <= quartiles[2]),
        ("Q4 (wide)", lambda w: w > quartiles[2])]

print(f"\n{'Quartile':<16} {'Trades':>6} {'WR':>6} {'Avg PnL':>10} {'Total':>10}")
print("-" * 55)
for label, pred in bins:
    subset = [t for t in trades if pred(t.bb_width)]
    if not subset:
        continue
    wins = sum(1 for t in subset if t.pnl > 0)
    wr = wins / len(subset) * 100
    avg = np.mean([t.pnl for t in subset]) * 100
    total = sum(t.pnl for t in subset) * 100
    print(f"{label:<16} {len(subset):>6} {wr:>5.1f}% {avg:>+9.3f}% {total:>+9.1f}%")

# Test different thresholds
print(f"\n{'='*70}")
print(f"BB Width Filter Sweep (only trade when BB width > threshold)")
print(f"{'='*70}")
print(f"{'Threshold':<12} {'Trades':>6} {'Filtered':>8} {'WR':>6} {'Avg PnL':>10} {'Total':>10} {'MaxDD':>8}")
print("-" * 70)

# No filter baseline
wins_all = sum(1 for t in trades if t.pnl > 0)
wr_all = wins_all / len(trades) * 100
avg_all = np.mean([t.pnl for t in trades]) * 100
total_all = sum(t.pnl for t in trades) * 100

# Simple max drawdown
def max_drawdown(trade_list):
    equity = 1.0
    peak = 1.0
    max_dd = 0
    for t in trade_list:
        equity *= (1 + t.pnl)
        peak = max(peak, equity)
        dd = (peak - equity) / peak
        max_dd = max(max_dd, dd)
    return max_dd * 100

mdd_all = max_drawdown(trades)
print(f"{'No filter':<12} {len(trades):>6} {'0':>8} {wr_all:>5.1f}% {avg_all:>+9.3f}% {total_all:>+9.1f}% {mdd_all:>7.1f}%")

for threshold in [0.01, 0.015, 0.02, 0.025, 0.03, 0.035, 0.04, 0.045, 0.05, 0.06, 0.07, 0.08]:
    filtered = [t for t in trades if t.bb_width > threshold]
    if len(filtered) < 10:
        continue
    removed = len(trades) - len(filtered)
    wins = sum(1 for t in filtered if t.pnl > 0)
    wr = wins / len(filtered) * 100
    avg = np.mean([t.pnl for t in filtered]) * 100
    total = sum(t.pnl for t in filtered) * 100
    mdd = max_drawdown(filtered)
    print(f"{threshold:<12.3f} {len(filtered):>6} {removed:>8} {wr:>5.1f}% {avg:>+9.3f}% {total:>+9.1f}% {mdd:>7.1f}%")

# Recent 104 days analysis
print(f"\n{'='*70}")
print(f"Same analysis — RECENT 104 DAYS ONLY")
print(f"{'='*70}")

recent_cutoff = len(candles) - 5003
recent_trades = [t for t in trades if t.entry_idx >= recent_cutoff]
print(f"Recent trades: {len(recent_trades)}")

if recent_trades:
    print(f"\n{'Threshold':<12} {'Trades':>6} {'Filtered':>8} {'WR':>6} {'Avg PnL':>10} {'Total':>10}")
    print("-" * 60)
    
    wins_r = sum(1 for t in recent_trades if t.pnl > 0)
    wr_r = wins_r / len(recent_trades) * 100
    avg_r = np.mean([t.pnl for t in recent_trades]) * 100
    total_r = sum(t.pnl for t in recent_trades) * 100
    print(f"{'No filter':<12} {len(recent_trades):>6} {'0':>8} {wr_r:>5.1f}% {avg_r:>+9.3f}% {total_r:>+9.1f}%")
    
    for threshold in [0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08]:
        filtered = [t for t in recent_trades if t.bb_width > threshold]
        if len(filtered) < 3:
            continue
        removed = len(recent_trades) - len(filtered)
        wins = sum(1 for t in filtered if t.pnl > 0)
        wr = wins / len(filtered) * 100
        avg = np.mean([t.pnl for t in filtered]) * 100
        total = sum(t.pnl for t in filtered) * 100
        print(f"{threshold:<12.3f} {len(filtered):>6} {removed:>8} {wr:>5.1f}% {avg:>+9.3f}% {total:>+9.1f}%")

# SOL analysis
print(f"\n{'='*70}")
print(f"SOL BB Width Filter Analysis")
print(f"{'='*70}")

os.environ['OKX_BB_CONFIG_DIR'] = '/home/xqianliu/.openclaw/workspace/trading/okx_sol_bb/config'
from okx_bb.config import load_config as load_config_fresh
import importlib
import okx_bb.config
importlib.reload(okx_bb.config)
sol_cfg = okx_bb.config.load_config()
sol_candles = load_or_fetch_candles(sol_cfg)
sol_days = (sol_candles[-1]['ts'] - sol_candles[0]['ts']) / 86400000
print(f"SOL Data: {len(sol_candles)} candles ({sol_days:.0f} days)")

sol_closes = np.array([c['c'] for c in sol_candles])
sol_bb_widths = np.zeros(len(sol_closes))
sol_period = sol_cfg.strategy.bb_period
for i in range(sol_period, len(sol_closes)):
    window = sol_closes[i-sol_period+1:i+1]
    mid = np.mean(window)
    std = np.std(window, ddof=0)
    sol_bb_widths[i] = (2 * sol_cfg.strategy.bb_multiplier * std) / mid

sol_trades = backtest_close_confirm_buffer(sol_candles, sol_cfg)
for t in sol_trades:
    t.bb_width = sol_bb_widths[t.entry_idx] if t.entry_idx < len(sol_bb_widths) else 0

print(f"SOL Total trades: {len(sol_trades)}")
if sol_trades:
    sol_widths = [t.bb_width for t in sol_trades]
    print(f"BB Width: min={min(sol_widths):.4f} median={np.median(sol_widths):.4f} max={max(sol_widths):.4f}")
    
    print(f"\n{'Threshold':<12} {'Trades':>6} {'Filtered':>8} {'WR':>6} {'Avg PnL':>10} {'Total':>10}")
    print("-" * 60)
    
    wins_s = sum(1 for t in sol_trades if t.pnl > 0)
    avg_s = np.mean([t.pnl for t in sol_trades]) * 100
    total_s = sum(t.pnl for t in sol_trades) * 100
    print(f"{'No filter':<12} {len(sol_trades):>6} {'0':>8} {wins_s/len(sol_trades)*100:>5.1f}% {avg_s:>+9.3f}% {total_s:>+9.1f}%")
    
    for threshold in [0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.10, 0.12]:
        filtered = [t for t in sol_trades if t.bb_width > threshold]
        if len(filtered) < 3:
            continue
        removed = len(sol_trades) - len(filtered)
        wins = sum(1 for t in filtered if t.pnl > 0)
        wr = wins / len(filtered) * 100
        avg = np.mean([t.pnl for t in filtered]) * 100
        total = sum(t.pnl for t in filtered) * 100
        print(f"{threshold:<12.3f} {len(filtered):>6} {removed:>8} {wr:>5.1f}% {avg:>+9.3f}% {total:>+9.1f}%")
