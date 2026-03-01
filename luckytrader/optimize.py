#!/usr/bin/env python3
"""
月度策略优化 - 每月1号自动运行
1. 拉取最长可用的30分钟K线数据
2. 扫描SL/TP/持仓时间参数
3. 找最优组合
4. 与当前参数对比
5. 输出优化建议报告
"""
import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from luckytrader.signal import get_candles
from luckytrader.strategy import detect_signal
from luckytrader.config import get_config, get_workspace_dir

SYSTEM_FILE = Path(__file__).parent.parent / "memory" / "trading" / "SYSTEM.md"

# 当前参数 — 从 config/params.toml 加载
_cfg = get_config()
CURRENT = {
    "sl": _cfg.risk.stop_loss_pct,
    "tp": _cfg.risk.take_profit_pct,
    "hold": _cfg.risk.max_hold_hours * 2,  # convert hours to 30m bars
}

# 交易成本
FEE_ROUND_TRIP_PCT = 8.64 / 10000  # 0.000864

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
                return {'pnl_pct': -stop_pct * 100 - FEE_ROUND_TRIP_PCT * 100, 'reason': 'STOP', 'bars': j}
            if highs[idx] >= tp:
                return {'pnl_pct': tp_pct * 100 - FEE_ROUND_TRIP_PCT * 100, 'reason': 'TP', 'bars': j}
        else:
            if highs[idx] >= stop:
                return {'pnl_pct': -stop_pct * 100 - FEE_ROUND_TRIP_PCT * 100, 'reason': 'STOP', 'bars': j}
            if lows[idx] <= tp:
                return {'pnl_pct': tp_pct * 100 - FEE_ROUND_TRIP_PCT * 100, 'reason': 'TP', 'bars': j}
    
    exit_idx = min(entry_idx + max_hold, len(closes) - 1)
    if direction == 'LONG':
        pnl = (closes[exit_idx] - entry) / entry * 100
    else:
        pnl = (entry - closes[exit_idx]) / entry * 100
    return {'pnl_pct': pnl - FEE_ROUND_TRIP_PCT * 100, 'reason': 'TIMEOUT', 'bars': exit_idx - entry_idx}

def run_backtest(candles_30m, sl, tp, hold, candles_4h=None, cfg=None):
    """参数扫描回测 — 使用 strategy.detect_signal() 生成信号"""
    if cfg is None:
        cfg = _cfg
    closes = [float(c['c']) for c in candles_30m]
    opens = [float(c['o']) for c in candles_30m]
    highs = [float(c['h']) for c in candles_30m]
    lows = [float(c['l']) for c in candles_30m]

    start = max(cfg.strategy.range_bars, cfg.strategy.lookback_bars) + 2
    trades = []
    in_trade_until = 0

    for i in range(start, len(candles_30m) - 1):
        if i <= in_trade_until:
            continue

        signal = detect_signal(candles_30m, candles_4h or [], i, cfg)
        if signal:
            entry_price = opens[i + 1]
            t = simulate_trade(signal, entry_price, i + 1, highs, lows, closes, sl, tp, hold)
            if t:
                trades.append(t)
                in_trade_until = i + 1 + t.get('bars', hold)
    
    if not trades:
        return {"count": 0, "total": 0, "avg": 0, "winrate": 0}
    
    wins = [t for t in trades if t['pnl_pct'] > 0]
    total = sum(t['pnl_pct'] for t in trades)
    return {
        "count": len(trades),
        "total": round(total, 2),
        "avg": round(total / len(trades), 3),
        "winrate": round(len(wins) / len(trades) * 100, 1),
    }

def optimize():
    # 拉最长数据
    candles = get_candles('BTC', '30m', 24 * 180)
    candles_4h = get_candles('BTC', '4h', 24 * 180 // 8)
    days = len(candles) / 48
    print(f"数据: {len(candles)} 根30分钟K线 ({days:.0f}天)\n")
    
    # 参数空间
    sls = [0.02, 0.025, 0.03, 0.035, 0.04, 0.05]
    tps = [0.03, 0.04, 0.05, 0.06, 0.07, 0.10]
    holds = [24, 48, 72, 96, 144]  # 12h, 24h, 36h, 48h, 72h
    
    # 当前参数表现
    current_result = run_backtest(candles, CURRENT["sl"], CURRENT["tp"], CURRENT["hold"], candles_4h=candles_4h)
    print(f"当前参数 (SL{CURRENT['sl']*100}% TP{CURRENT['tp']*100}% {CURRENT['hold']*0.5:.0f}h):")
    print(f"  {current_result['count']}笔 | 胜率{current_result['winrate']}% | 总{current_result['total']:+.1f}% | 每笔{current_result['avg']:+.3f}%")
    
    # 全量扫描（信号由 detect_signal 统一生成，只扫描 SL/TP/持仓时间）
    best = {"avg": -999, "params": None, "result": None}
    all_results = []
    
    for sl in sls:
        for tp in tps:
            if tp <= sl:
                continue
            for hold in holds:
                r = run_backtest(candles, sl, tp, hold, candles_4h=candles_4h)
                if r["count"] >= 20:
                    all_results.append({"sl": sl, "tp": tp, "hold": hold, **r})
                    if r["avg"] > best["avg"]:
                        best = {"avg": r["avg"], "params": {"sl": sl, "tp": tp, "hold": hold}, "result": r}
    
    # 排序输出 Top 10
    all_results.sort(key=lambda x: -x["avg"])
    
    print(f"\n{'='*70}")
    print("Top 10 参数组合:")
    print(f"{'='*70}")
    for i, r in enumerate(all_results[:10]):
        is_current = (r["sl"] == CURRENT["sl"] and r["tp"] == CURRENT["tp"]
                      and r["hold"] == CURRENT["hold"])
        marker = " ⭐" if is_current else ""
        print(f"  {i+1}. SL{r['sl']*100:.1f}% TP{r['tp']*100:.0f}% {r['hold']*0.5:.0f}h: {r['count']}笔 | 胜率{r['winrate']}% | 总{r['total']:+.1f}% | 每笔{r['avg']:+.3f}%{marker}")
    
    # 对比
    if best["params"]:
        bp = best["params"]
        br = best["result"]
        improvement = (br["avg"] - current_result["avg"]) / abs(current_result["avg"]) * 100 if current_result["avg"] != 0 else 0
        
        print(f"\n{'='*70}")
        print("优化建议:")
        print(f"{'='*70}")
        print(f"  最优: SL{bp['sl']*100:.1f}% TP{bp['tp']*100:.0f}% {bp['hold']*0.5:.0f}h")
        print(f"  期望: {br['avg']:+.3f}%/笔 (当前 {current_result['avg']:+.3f}%/笔)")
        print(f"  提升: {improvement:+.1f}%")
        
        if improvement > 30:
            print(f"\n  ✅ 建议更新参数 (提升>{30}%)")
            print(f"  建议参数: SL={bp['sl']}, TP={bp['tp']}, HOLD={bp['hold']} ({bp['hold']*0.5:.0f}h)")
            print(f"  ⚠️  此为建议，不会自动修改生产配置。请人工评估后手动更新 config.toml")
        else:
            print(f"\n  ⏸️ 保持当前参数 (提升不足30%)")
    
    return {
        "data_days": days,
        "data_candles": len(candles),
        "current": {"params": CURRENT, "result": current_result},
        "best": best,
        "top10": all_results[:10],
    }

if __name__ == "__main__":
    result = optimize()
    
    # 保存到月度建议日志（只记录建议，不修改生产配置）
    suggestions_dir = get_workspace_dir() / "memory" / "trading" / "optimization_suggestions"
    suggestions_dir.mkdir(parents=True, exist_ok=True)
    
    month_str = datetime.now(timezone.utc).strftime("%Y-%m")
    suggestion_file = suggestions_dir / f"{month_str}.json"
    
    # 追加到当月文件
    history = []
    if suggestion_file.exists():
        try:
            history = json.loads(suggestion_file.read_text())
        except Exception as e:
            print(f"⚠️ 读取历史建议失败: {e}")
            history = []
    
    history.append({
        "date": datetime.now(timezone.utc).isoformat(),
        "status": "SUGGESTION_ONLY",
        "note": "此为优化建议，未自动应用。需人工评估 + canonical backtest 验证后手动更新 config.toml",
        "result": result,
    })
    
    suggestion_file.write_text(json.dumps(history, indent=2, default=str))
    print(f"\n建议已保存到 {suggestion_file}")
    print(f"⚠️  注意：此为建议，不会自动修改生产配置 config.toml")
