#!/usr/bin/env python3
"""
月度策略优化 v2 — 多策略 + Out-of-Sample + Regime 分层 + 常识关卡

支持的策略：
- BTC: HL Momentum (luckytrader/strategy.py) → HL exchange
- ETH: BB Breakout (okx_bb/strategy.py) → OKX exchange
- SOL: BB Breakout (okx_sol_bb/) → OKX exchange

每月1号自动运行，输出建议报告，不自动改配置。
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from luckytrader.signal import get_candles
from luckytrader.strategy import detect_signal as hl_detect_signal
from luckytrader.config import get_config, get_coin_config, get_workspace_dir, TRADING_COINS, TradingConfig
from luckytrader.backtest import simulate_trade
from luckytrader.regime import compute_de

# BB canonical backtest import (okx_bb lives in the trading workspace)
_bb_available = False
try:
    # Add trading dir to path for okx_bb imports
    for _p in [str(Path(__file__).parent.parent.parent),
               str(Path.home() / ".openclaw" / "workspace" / "trading")]:
        if _p not in sys.path:
            sys.path.insert(0, _p)
    from okx_bb.backtest import backtest_close_confirm_buffer as bb_canonical_backtest
    from okx_bb.config import OKXConfig, StrategyConfig as BBStrategyConfig, RiskConfig as BBRiskConfig, FeeConfig as BBFeeConfig, ExecutionConfig as BBExecutionConfig
    _bb_available = True
except ImportError:
    pass

# Strategy routing: which coin uses which strategy
STRATEGY_MAP = {
    "BTC": "hl_momentum",
    "ETH": "bb_breakout",
    "SOL": "bb_breakout",
}

# OKX BB fee: 10 bps round-trip (5 bps/side taker)
OKX_FEE_RT_PCT = 0.0010
# HL fee: 8.64 bps round-trip
HL_FEE_RT_PCT = 0.000864

_cfg = get_config()


def run_backtest_hl(candles_30m, candles_4h, sl, tp, hold, cfg, coin_cfg=None):
    """Run HL Momentum backtest on a slice. Returns trade list."""
    closes = [float(c['c']) for c in candles_30m]
    opens = [float(c['o']) for c in candles_30m]
    highs = [float(c['h']) for c in candles_30m]
    lows = [float(c['l']) for c in candles_30m]

    if coin_cfg:
        range_bars = coin_cfg.range_bars
        lookback_bars = coin_cfg.lookback_bars
    else:
        range_bars = cfg.strategy.range_bars
        lookback_bars = cfg.strategy.lookback_bars

    start = max(range_bars, lookback_bars) + 2  # align with backtest.run_backtest
    trades = []
    in_trade_until = 0

    for i in range(start, len(candles_30m) - 1):
        if i <= in_trade_until:
            continue

        signal = hl_detect_signal(candles_30m, candles_4h or [], i, cfg, coin_cfg)
        if signal:
            entry_price = opens[i + 1]
            t = simulate_trade(signal, entry_price, i + 1, highs, lows, closes, sl, tp, hold)
            if t:
                t['entry_idx'] = i + 1
                trades.append(t)
                in_trade_until = i + 1 + t.get('bars', hold)

    return trades


def run_backtest_bb(candles_30m, sl, tp, hold, bb_period, bb_mult,
                    trend_period, trend_lookback, fee_rt_pct=OKX_FEE_RT_PCT):
    """Run BB Breakout backtest using the CANONICAL production backtest.
    
    Directly calls okx_bb/backtest.py:backtest_close_confirm_buffer —
    same entry logic, same entry price, same fees as production.
    """
    if not _bb_available:
        print("  ⚠️ okx_bb not available, skipping BB backtest")
        return []

    # Build OKXConfig with scan parameters
    cfg = OKXConfig(
        strategy=BBStrategyConfig(
            bb_period=bb_period, bb_multiplier=bb_mult,
            trend_ema_period=trend_period, trend_lookback=trend_lookback,
        ),
        risk=BBRiskConfig(
            stop_loss_pct=sl, take_profit_pct=tp, max_hold_bars=hold,
        ),
        fees=BBFeeConfig(
            taker_fee=0.0005, maker_fee=0.0002,  # OKX VIP0: maker 2bps, taker 5bps
        ),
        execution=BBExecutionConfig(entry_buffer_pct=0.0),
    )

    # Convert candle format: canonical expects float values, not strings
    candles = [{'o': float(c['o']), 'h': float(c['h']), 'l': float(c['l']),
                'c': float(c['c']), 'v': float(c.get('v', 0)), 't': c.get('t', 0),
                'ts': c.get('t', 0)}
               for c in candles_30m]

    # Run canonical backtest — EXACT same logic as production
    bb_trades = bb_canonical_backtest(candles, cfg)

    # Convert Trade objects to dict format for compute_stats compatibility
    trades = []
    for t in bb_trades:
        trades.append({
            'dir': t.direction,
            'pnl_pct': t.pnl * 100,  # canonical uses ratio, we use percentage
            'bars': t.exit_idx - t.entry_idx,
            'reason': t.reason.upper(),
            'entry_idx': t.entry_idx,
        })
    return trades


# Backward compat alias
run_backtest_on_slice = run_backtest_hl


def _run_backtest(strategy_type, candles_30m, candles_4h, sl, tp, hold,
                  cfg=None, coin_cfg=None,
                  bb_period=None, bb_mult=None, trend_period=None, trend_lookback=None):
    """Route to correct backtest based on strategy type."""
    if strategy_type == "bb_breakout":
        return run_backtest_bb(candles_30m, sl, tp, hold,
                               bb_period, bb_mult, trend_period, trend_lookback,
                               fee_rt_pct=OKX_FEE_RT_PCT)
    else:
        return run_backtest_hl(candles_30m, candles_4h, sl, tp, hold,
                               cfg or _cfg, coin_cfg)


def compute_stats(trades):
    """Compute stats from trade list."""
    if not trades:
        return {"count": 0, "total": 0, "avg": 0, "winrate": 0, "max_dd": 0}

    wins = [t for t in trades if t['pnl_pct'] > 0]
    total = sum(t['pnl_pct'] for t in trades)

    # Max drawdown (cumulative)
    cum = 0
    peak = 0
    max_dd = 0
    for t in trades:
        cum += t['pnl_pct']
        peak = max(peak, cum)
        dd = peak - cum
        max_dd = max(max_dd, dd)

    return {
        "count": len(trades),
        "total": round(total, 2),
        "avg": round(total / len(trades), 3),
        "winrate": round(len(wins) / len(trades) * 100, 1),
        "max_dd": round(max_dd, 2),
    }


def tag_regime(candles_30m, trades, de_lookback_days=7):
    """Tag each trade with regime (range/trend) based on DE at entry.
    
    Aggregates 30m candles into daily candles for DE computation.
    """
    for t in trades:
        idx = t.get('entry_idx', 0)
        bars_needed = de_lookback_days * 48 + 48  # extra day for safety
        if idx >= bars_needed:
            # Aggregate 30m into daily: group by 48-bar blocks
            slice_30m = candles_30m[idx - bars_needed:idx]
            daily = []
            for d in range(0, len(slice_30m) - 47, 48):
                block = slice_30m[d:d+48]
                daily.append({
                    'h': max(float(c['h']) for c in block),
                    'l': min(float(c['l']) for c in block),
                    'c': float(block[-1]['c']),
                })
            try:
                de = compute_de(daily, de_lookback_days)
                if de is not None:
                    t['regime'] = 'trend' if de > 0.25 else 'range'
                    t['de'] = round(de, 3)
                else:
                    t['regime'] = 'unknown'
            except Exception as e:
                t['regime'] = 'unknown'
                # DE computation failed — non-critical, continue
        else:
            t['regime'] = 'unknown'


def sanity_check(best_params, best_stats, current_stats, coin):
    """Sanity gate — flag suspicious results."""
    warnings = []

    if best_stats["count"] < 20:
        warnings.append(f"样本量不足: {best_stats['count']}笔 (<20)")

    if best_stats["avg"] > 2.0:
        warnings.append(f"每笔收益 {best_stats['avg']:+.3f}% 异常高 (>2%)，可能过拟合")

    improvement = 0
    if current_stats["avg"] != 0:
        improvement = (best_stats["avg"] - current_stats["avg"]) / abs(current_stats["avg"]) * 100
    if improvement > 500:
        warnings.append(f"提升 {improvement:.0f}% 异常大 (>500%)，可能过拟合")

    # Risk/reward sanity
    sl = best_params["sl"]
    tp = best_params["tp"]
    wr = best_stats["winrate"] / 100
    expected = wr * tp - (1 - wr) * sl
    if expected < 0:
        warnings.append(f"期望值为负: {wr:.0%}×{tp*100:.1f}% - {1-wr:.0%}×{sl*100:.1f}% = {expected*100:+.2f}%")

    if best_stats["max_dd"] > 15:
        warnings.append(f"最大回撤 {best_stats['max_dd']:.1f}% 偏大 (>15%)")

    return warnings


def _load_bb_config(coin):
    """Load BB strategy config for ETH/SOL."""
    import tomllib
    # okx_bb and okx_sol_bb live in the trading workspace dir
    workspace = get_workspace_dir()  # ~/.openclaw/workspace
    pkg_name = "okx_bb" if coin == "ETH" else "okx_sol_bb"
    candidates = [
        workspace / "trading" / pkg_name / "config",
        workspace / pkg_name / "config",
        Path(__file__).parent.parent.parent / pkg_name / "config",
    ]
    for cfg_dir in candidates:
        cfg_file = cfg_dir / "config.toml"
        if cfg_file.exists():
            with open(cfg_file, "rb") as f:
                return tomllib.load(f)
    return None


def optimize_coin(coin, days=180):
    """Optimize one coin with train/test split."""
    strategy_type = STRATEGY_MAP.get(coin, "hl_momentum")

    print(f"\n{'='*70}")
    print(f"  {coin} 优化 [{strategy_type}]")
    print(f"{'='*70}")

    # Load current params based on strategy type
    if strategy_type == "bb_breakout":
        bb_cfg = _load_bb_config(coin)
        if not bb_cfg:
            print(f"  ⚠️ 无法加载 {coin} BB 配置")
            return None
        current_sl = bb_cfg["risk"]["stop_loss_pct"]
        current_tp = bb_cfg["risk"]["take_profit_pct"]
        current_hold = bb_cfg["risk"].get("max_hold_bars", 120)
        bb_period = bb_cfg["strategy"]["bb_period"]
        bb_mult = bb_cfg["strategy"]["bb_multiplier"]
        trend_period = bb_cfg["strategy"].get("trend_ema_period", 96)
        trend_lookback = bb_cfg["strategy"].get("trend_lookback", 8)
        coin_cfg = None
    else:
        try:
            coin_cfg = get_coin_config(coin)
        except Exception:
            coin_cfg = None
        if coin_cfg:
            current_sl = coin_cfg.stop_loss_pct
            current_tp = coin_cfg.take_profit_pct
            current_hold = int(getattr(coin_cfg, 'max_hold_hours', _cfg.risk.max_hold_hours) * 2)
        else:
            current_sl = _cfg.risk.stop_loss_pct
            current_tp = _cfg.risk.take_profit_pct
            current_hold = int(_cfg.risk.max_hold_hours * 2)
        bb_period = bb_mult = trend_period = trend_lookback = None

    coin_sym = coin

    # Download data
    candles_30m = get_candles(coin_sym, '30m', 24 * days)
    candles_4h = get_candles(coin_sym, '4h', 24 * days // 8) if strategy_type == "hl_momentum" else []

    if not candles_30m or len(candles_30m) < 200:
        print(f"  ⚠️ 数据不足: {len(candles_30m) if candles_30m else 0} 根 30m")
        return None

    total_bars = len(candles_30m)
    data_days = total_bars / 48
    print(f"  数据: {total_bars} 根 30m ({data_days:.0f}天)")

    # Train/test split: 70/30
    split_idx = int(total_bars * 0.7)
    train_30m = candles_30m[:split_idx]
    test_30m = candles_30m[split_idx:]
    # 4h: include overlap for test set (trend EMA needs ~48 bars warmup)
    # Training uses first 70%, test uses last 30% but with 60-bar 4h overlap
    split_4h = int(len(candles_4h) * 0.7) if candles_4h else 0
    train_4h = candles_4h[:split_4h] if candles_4h else []
    overlap_4h = 60  # bars of 4h to carry over for EMA warmup
    test_4h_start = max(0, split_4h - overlap_4h)
    test_4h = candles_4h[test_4h_start:] if candles_4h else []

    train_days = len(train_30m) / 48
    test_days = len(test_30m) / 48
    print(f"  训练集: {train_days:.0f}天 | 测试集: {test_days:.0f}天")

    # Shared kwargs for BB routing
    _bb_kw = dict(bb_period=bb_period, bb_mult=bb_mult,
                  trend_period=trend_period, trend_lookback=trend_lookback)

    # Current params on full data
    current_trades = _run_backtest(strategy_type, candles_30m, candles_4h,
                                   current_sl, current_tp, current_hold,
                                   _cfg, coin_cfg, **_bb_kw)
    current_stats = compute_stats(current_trades)
    print(f"\n  当前参数 (SL{current_sl*100:.1f}% TP{current_tp*100:.1f}% {current_hold*0.5:.0f}h):")
    print(f"    {current_stats['count']}笔 | 胜率{current_stats['winrate']}% | 总{current_stats['total']:+.1f}% | 每笔{current_stats['avg']:+.3f}% | 最大回撤{current_stats['max_dd']:.1f}%")

    # Parameter space
    sls = [0.02, 0.025, 0.03, 0.035, 0.04, 0.05]
    tps = [0.015, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.10]
    holds = [24, 48, 72, 96, 120, 144]  # 12h-72h

    # Scan on TRAINING set
    print(f"\n  扫描参数空间 ({len(sls)}×{len(tps)}×{len(holds)} 组合)...")
    sys.stdout.flush()

    train_results = []
    for sl in sls:
        for tp in tps:
            for hold in holds:
                trades = _run_backtest(strategy_type, train_30m, train_4h, sl, tp, hold, _cfg, coin_cfg, **_bb_kw)
                stats = compute_stats(trades)
                if stats["count"] >= 10:
                    train_results.append({"sl": sl, "tp": tp, "hold": hold, **stats})

    if not train_results:
        print("  ⚠️ 训练集无有效结果")
        return None

    train_results.sort(key=lambda x: -x["avg"])

    # Top 5 from training → validate on TEST set
    print(f"\n  训练集 Top 5 → 测试集验证:")
    print(f"  {'#':>3} | {'SL':>5} | {'TP':>5} | {'Hold':>5} | {'Train Avg':>10} | {'Test Avg':>10} | {'Test WR':>7} | {'Test #':>6} | {'Test DD':>7}")
    print(f"  {'-'*80}")

    validated = []
    for i, r in enumerate(train_results[:5]):
        test_trades = _run_backtest(strategy_type, test_30m, test_4h, r["sl"], r["tp"], r["hold"], _cfg, coin_cfg, **_bb_kw)
        test_stats = compute_stats(test_trades)

        # Tag regime on test trades
        tag_regime(test_30m, test_trades)
        range_trades = [t for t in test_trades if t.get('regime') == 'range']
        trend_trades = [t for t in test_trades if t.get('regime') == 'trend']
        range_stats = compute_stats(range_trades)
        trend_stats = compute_stats(trend_trades)

        train_avg = r["avg"]
        test_avg = test_stats["avg"]
        marker = "✅" if test_avg > 0 else "❌"
        # Degradation check: test should be within 50% of train
        if train_avg > 0 and test_avg > 0:
            degradation = (train_avg - test_avg) / train_avg * 100
            if degradation > 70:
                marker = "⚠️"  # Heavy degradation

        print(f"  {i+1:>3} | {r['sl']*100:>4.1f}% | {r['tp']*100:>4.1f}% | {r['hold']*0.5:>4.0f}h | {train_avg:>+9.3f}% | {test_avg:>+9.3f}% | {test_stats['winrate']:>6.1f}% | {test_stats['count']:>5} | {test_stats['max_dd']:>6.1f}% {marker}")

        validated.append({
            "sl": r["sl"], "tp": r["tp"], "hold": r["hold"],
            "train": {"avg": train_avg, "count": r["count"], "winrate": r["winrate"]},
            "test": test_stats,
            "regime": {"range": range_stats, "trend": trend_stats},
        })

    # Select best: positive on BOTH train and test
    best = None
    for v in validated:
        if v["train"]["avg"] > 0 and v["test"]["avg"] > 0:
            if best is None or v["test"]["avg"] > best["test"]["avg"]:
                best = v

    # Regime breakdown for best
    if best:
        print(f"\n  最优参数 Regime 分析:")
        print(f"    横盘: {best['regime']['range']['count']}笔 | {best['regime']['range']['avg']:+.3f}%/笔 | 胜率{best['regime']['range']['winrate']}%")
        print(f"    趋势: {best['regime']['trend']['count']}笔 | {best['regime']['trend']['avg']:+.3f}%/笔 | 胜率{best['regime']['trend']['winrate']}%")

    # Sanity check — compare against current params ON TEST SET for fair comparison
    if best:
        best_params = {"sl": best["sl"], "tp": best["tp"], "hold": best["hold"]}
        current_test_for_sanity = compute_stats(
            _run_backtest(strategy_type, test_30m, test_4h, current_sl, current_tp, current_hold, _cfg, coin_cfg, **_bb_kw)
        )
        warnings = sanity_check(best_params, best["test"], current_test_for_sanity, coin)
        if warnings:
            print(f"\n  ⚠️ 常识关卡警告:")
            for w in warnings:
                print(f"    - {w}")

    # Recommendation
    print(f"\n  {'='*60}")
    print(f"  {coin} 优化建议:")
    print(f"  {'='*60}")

    if not best:
        print(f"  ⏸️ 无参数在训练集和测试集都为正。保持当前参数。")
        return {
            "coin": coin, "data_days": data_days,
            "current": {"sl": current_sl, "tp": current_tp, "hold": current_hold, "stats": current_stats},
            "recommendation": "KEEP",
            "reason": "No params positive on both train and test",
        }

    bp = best
    test_improvement = 0
    if current_stats["avg"] != 0:
        # Run current params on test set for fair comparison
        current_test_trades = _run_backtest(strategy_type, test_30m, test_4h, current_sl, current_tp, current_hold, _cfg, coin_cfg, **_bb_kw)
        current_test_stats = compute_stats(current_test_trades)
        if current_test_stats["avg"] != 0:
            test_improvement = (bp["test"]["avg"] - current_test_stats["avg"]) / abs(current_test_stats["avg"]) * 100
        else:
            test_improvement = 999 if bp["test"]["avg"] > 0 else 0
        print(f"  当前参数测试集: {current_test_stats['count']}笔 | {current_test_stats['avg']:+.3f}%/笔")
    else:
        current_test_stats = current_stats

    print(f"  最优参数: SL{bp['sl']*100:.1f}% TP{bp['tp']*100:.1f}% {bp['hold']*0.5:.0f}h")
    print(f"  训练集: {bp['train']['avg']:+.3f}%/笔 ({bp['train']['count']}笔)")
    print(f"  测试集: {bp['test']['avg']:+.3f}%/笔 ({bp['test']['count']}笔, 胜率{bp['test']['winrate']}%)")
    print(f"  测试集提升: {test_improvement:+.1f}% vs 当前参数")

    if test_improvement > 30 and bp["test"]["count"] >= 10 and not warnings:
        print(f"\n  ✅ 建议更新参数")
        recommendation = "UPDATE"
    elif warnings:
        print(f"\n  ⚠️ 有常识关卡警告，建议人工评估后决定")
        recommendation = "REVIEW"
    else:
        print(f"\n  ⏸️ 保持当前参数 (测试集提升不足30%)")
        recommendation = "KEEP"

    print(f"  ⚠️ 此为建议，不会自动修改生产配置。")

    return {
        "coin": coin, "data_days": data_days,
        "current": {"sl": current_sl, "tp": current_tp, "hold": current_hold, "stats": current_stats},
        "best": {"sl": bp["sl"], "tp": bp["tp"], "hold": bp["hold"],
                 "train": bp["train"], "test": bp["test"], "regime": bp["regime"]},
        "recommendation": recommendation,
        "warnings": warnings if best else [],
        "test_improvement": test_improvement,
    }


def optimize():
    """Run optimization for all trading coins."""
    print("="*70)
    print("月度策略优化 v2")
    print(f"时间: {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}")
    all_coins = list(STRATEGY_MAP.keys())  # BTC, ETH, SOL
    print(f"币种: {', '.join(all_coins)}")
    print("="*70)

    results = {}
    for coin in all_coins:
        try:
            r = optimize_coin(coin, days=180)
            if r:
                results[coin] = r
        except Exception as e:
            print(f"\n  ❌ {coin} 优化失败: {e}")
            import traceback
            traceback.print_exc()

    # Summary
    print(f"\n\n{'='*70}")
    print("汇总")
    print(f"{'='*70}")
    for coin, r in results.items():
        rec = r.get("recommendation", "?")
        curr = r.get("current", {})
        best = r.get("best", {})
        icon = {"UPDATE": "✅", "REVIEW": "⚠️", "KEEP": "⏸️"}.get(rec, "?")
        print(f"  {icon} {coin}: {rec}")
        if best:
            print(f"    当前: SL{curr['sl']*100:.1f}% TP{curr['tp']*100:.1f}% {curr['hold']*0.5:.0f}h → 建议: SL{best['sl']*100:.1f}% TP{best['tp']*100:.1f}% {best['hold']*0.5:.0f}h")
            t = best.get("test", {})
            print(f"    测试集: {t.get('avg', 0):+.3f}%/笔, {t.get('count', 0)}笔, 胜率{t.get('winrate', 0)}%")
        warn = r.get("warnings", [])
        if warn:
            for w in warn:
                print(f"    ⚠️ {w}")

    return results


if __name__ == "__main__":
    results = optimize()

    # Save
    suggestions_dir = get_workspace_dir() / "memory" / "trading" / "optimization_suggestions"
    suggestions_dir.mkdir(parents=True, exist_ok=True)

    month_str = datetime.now(timezone.utc).strftime("%Y-%m")
    suggestion_file = suggestions_dir / f"{month_str}.json"

    history = []
    if suggestion_file.exists():
        try:
            history = json.loads(suggestion_file.read_text())
        except Exception:
            history = []

    history.append({
        "date": datetime.now(timezone.utc).isoformat(),
        "version": "v2",
        "status": "SUGGESTION_ONLY",
        "results": results,
    })

    suggestion_file.write_text(json.dumps(history, indent=2, default=str))
    print(f"\n建议已保存到 {suggestion_file}")
