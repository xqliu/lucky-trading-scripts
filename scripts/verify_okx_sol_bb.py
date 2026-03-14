#!/usr/bin/env python3
"""
OKX SOL BB 交易逻辑验证器（只读，不下单）
============================================
从交易所拉实时 K 线，用生产代码计算 BB 信号，
检查本地 state 与交易所仓位一致性。
"""
import sys
import json
import subprocess
from pathlib import Path
from datetime import datetime, timezone, timedelta

_root = str(Path(__file__).parent.parent)
if _root not in sys.path:
    sys.path.insert(0, _root)

from okx_sol_bb.strategy import detect_signal, get_bb_levels
from okx_sol_bb.config import load_config
from okx_sol_bb.executor import SolBBExecutor, POSITION_STATE_FILE


def get_recent_candles_from_exchange(inst_id: str, bar: str = "30m", limit: int = 300):
    import urllib.request
    url = f"https://www.okx.com/api/v5/market/candles?instId={inst_id}&bar={bar}&limit={limit}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    if data.get("code") != "0":
        raise RuntimeError(f"OKX API error: {data}")
    raw = data["data"]
    candles = []
    for c in reversed(raw):
        candles.append({
            "t": int(c[0]),
            "o": float(c[1]),
            "h": float(c[2]),
            "l": float(c[3]),
            "c": float(c[4]),
            "vol": float(c[5]),
        })
    return candles


def main():
    cfg = load_config()
    ex = SolBBExecutor(cfg)

    sgt = timezone(timedelta(hours=8))
    now = datetime.now(sgt)
    print(f"## 🟣 OKX SOL BB 逻辑验证 ({now:%H:%M} SGT)")
    print()

    # 1. Service status
    try:
        r = subprocess.run(["systemctl", "is-active", "okx-sol-bb-monitor.service"],
                           capture_output=True, text=True, timeout=5)
        mon = r.stdout.strip()
    except Exception:
        mon = "unknown"
    status_emoji = "🟢" if mon == "active" else "⚠️"
    print(f"**服务**: {status_emoji} {mon}")

    # 2. Account
    bal = ex.client.get_balance()
    equity = bal.get("total_equity", 0)
    print(f"**账户**: ${equity:.2f}")

    # 3. Price & BB
    candles = ex.fetch_candles(limit=300)
    closes = [c["c"] for c in candles]
    idx = len(closes) - 1
    current_price = closes[-1]

    bb = get_bb_levels(closes, cfg.strategy.bb_period, cfg.strategy.bb_multiplier, idx)
    if bb:
        mid, upper, lower = bb
        print(f"**SOL**: ${current_price:.2f} | BB({cfg.strategy.bb_period},{cfg.strategy.bb_multiplier}): "
              f"[{lower:.2f}, {mid:.2f}, {upper:.2f}]")
        dist_upper_pct = (upper - current_price) / current_price * 100
        dist_lower_pct = (current_price - lower) / current_price * 100
        print(f"**距离**: 上轨 {dist_upper_pct:+.1f}% | 下轨 -{dist_lower_pct:.1f}%")
    else:
        print(f"**SOL**: ${current_price:.2f} | BB: 计算失败")
    print()

    # 4. Signal check
    signal = detect_signal(closes, cfg.strategy.bb_period, cfg.strategy.bb_multiplier, idx)
    if signal:
        print(f"**信号**: ⚡ {signal}")
    else:
        print(f"**信号**: HOLD")

    # 5. Position consistency
    local_pos = ex.load_position()
    exchange_pos = ex.client.get_positions(cfg.instId)

    has_exchange = False
    if exchange_pos:
        has_exchange = any(float(p.get("pos", 0)) != 0 for p in exchange_pos)

    has_local = bool(local_pos)
    print()

    if has_exchange and has_local:
        p = next(pp for pp in exchange_pos if float(pp.get("pos", 0)) != 0)
        ex_dir = "LONG" if float(p.get("pos", 0)) > 0 else "SHORT"
        ex_size = abs(float(p.get("pos", 0)))
        ex_avg = float(p.get("avgPx", 0) or 0)
        upl = float(p.get("upl", 0) or 0)

        loc_dir = local_pos.get("direction")
        loc_entry = float(local_pos.get("entry_price", 0) or 0)

        dir_match = ex_dir == loc_dir
        price_diff = abs(ex_avg - loc_entry) / ex_avg * 100 if ex_avg else 0

        print(f"**仓位**: {ex_dir} {ex_size:g}张 @ ${ex_avg:.2f} | uPnL ${upl:+.2f}")
        sl = local_pos.get("sl_price")
        tp = local_pos.get("tp_price")
        if sl is not None:
            print(f"**SL**: ${float(sl):.2f} | **TP**: ${float(tp):.2f}" if tp else f"**SL**: ${float(sl):.2f}")

        # Check SL on exchange
        algos = ex.client.get_algo_orders(cfg.instId, "conditional")
        has_sl_on_exchange = any(a.get("slTriggerPx") for a in (algos or []))

        if dir_match and price_diff < 1 and has_sl_on_exchange:
            print("✅ 本地/交易所一致，SL 已设")
        else:
            issues = []
            if not dir_match:
                issues.append(f"方向不一致: 本地{loc_dir} vs 交易所{ex_dir}")
            if price_diff >= 1:
                issues.append(f"价格偏差 {price_diff:.1f}%")
            if not has_sl_on_exchange:
                issues.append("❌ 交易所无 SL！")
            print(f"⚠️ " + " | ".join(issues))

    elif not has_exchange and not has_local:
        print("✅ 空仓（本地/交易所一致）")
    elif has_exchange and not has_local:
        p = next(pp for pp in exchange_pos if float(pp.get("pos", 0)) != 0)
        print(f"❌ 交易所有仓位 ({float(p.get('pos',0)):g}张) 但本地无记录")
    else:
        print(f"❌ 本地有记录 ({local_pos.get('direction')}) 但交易所已空仓")


if __name__ == "__main__":
    main()
