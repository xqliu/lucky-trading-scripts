#!/usr/bin/env python3
"""
OKX SOL BB 交易逻辑验证器（只读，不下单）
格式与 verify_okx_bb.py 一致：紧凑 3 行输出
"""
import os
import sys
import subprocess
from pathlib import Path

_root = str(Path(__file__).parent.parent)
if _root not in sys.path:
    sys.path.insert(0, _root)

os.environ.setdefault("OKX_SOL_BB_CONFIG_DIR",
                       str(Path.home() / ".openclaw/workspace/trading/okx_sol_bb/config"))

from okx_sol_bb.strategy import detect_signal, get_bb_levels
from okx_sol_bb.config import load_config
from okx_sol_bb.executor import SolBBExecutor


def main():
    cfg = load_config()
    ex = SolBBExecutor(cfg)

    report = []
    has_issues = False

    # 1. Candles + BB
    candles = ex.fetch_candles(limit=300)
    if len(candles) < 30:
        report.append("❌ **OKX SOL BB**: 数据不足")
        print("\n".join(report))
        return 1

    closes = [c["c"] for c in candles]
    idx = len(closes) - 1
    price = closes[-1]

    bb = get_bb_levels(closes, cfg.strategy.bb_period, cfg.strategy.bb_multiplier, idx)
    if not bb:
        report.append("❌ **OKX SOL BB**: BB 计算失败")
        print("\n".join(report))
        return 1

    mid, upper, lower = bb

    # 2. Signal
    signal = detect_signal(closes, cfg.strategy.bb_period, cfg.strategy.bb_multiplier, idx)
    if signal:
        signal_str = f"🟢 {signal}" if signal == "LONG" else f"🔴 {signal}"
    else:
        dist_upper = (upper - price) / price * 100
        dist_lower = (price - lower) / price * 100
        signal_str = f"HOLD｜距上轨 +{dist_upper:.1f}%，距下轨 -{dist_lower:.1f}%"

    # 3. Position consistency
    local_pos = ex.load_position()
    exchange_pos = ex.client.get_positions(cfg.instId)
    has_exchange = exchange_pos and any(float(p.get("pos", 0)) != 0 for p in exchange_pos)
    has_local = bool(local_pos)

    pos_str = "无持仓"
    if has_exchange:
        p = next(pp for pp in exchange_pos if float(pp.get("pos", 0)) != 0)
        ex_dir = "LONG" if float(p.get("pos", 0)) > 0 else "SHORT"
        ex_avg = float(p.get("avgPx", 0) or 0)
        upl = float(p.get("upl", 0) or 0)

        sl_str = ""
        if has_local:
            sl = local_pos.get("sl_price")
            if sl is not None:
                sl_str = f", SL={float(sl):.2f}"
        pos_str = f"{ex_dir} @ {ex_avg:.2f}, uPnL ${upl:+.2f}{sl_str}"

        # Check consistency
        if has_local:
            loc_dir = local_pos.get("direction")
            if loc_dir != ex_dir:
                has_issues = True
                pos_str += " ⚠️方向不一致"
        else:
            has_issues = True
            pos_str += " ❌本地无记录"

        # Check SL on exchange
        algos = ex.client.get_algo_orders(cfg.instId, "conditional")
        if not any(a.get("slTriggerPx") for a in (algos or [])):
            has_issues = True
            pos_str += " ❌无SL"
    elif has_local and not has_exchange:
        has_issues = True
        pos_str = f"❌ 本地有记录但交易所空仓"

    # 4. Service
    try:
        svc = subprocess.run(["systemctl", "is-active", "okx-sol-bb-monitor"],
                             capture_output=True, text=True, timeout=5)
        svc_status = svc.stdout.strip()
    except Exception:
        svc_status = "unknown"

    if svc_status != "active":
        has_issues = True

    # Build compact report (same format as ETH)
    if not has_issues:
        report.append("✅ **OKX SOL BB 验证通过**")
    else:
        report.append("⚠️ **OKX SOL BB 验证有问题**")

    report.append(f"SOL {price:.2f}｜BB {lower:.2f}-{upper:.2f}｜{signal_str}")
    report.append(f"仓位: {pos_str}｜服务: {svc_status}")

    if svc_status != "active":
        report.append(f"❌ 服务异常: {svc_status}")

    print("\n".join(report))
    return 1 if has_issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
