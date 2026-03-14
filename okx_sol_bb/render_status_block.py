#!/usr/bin/env python3
"""Render OKX SOL BB status block for reports."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from okx_sol_bb.config import load_config
from okx_sol_bb.executor import SolBBExecutor


def fmt_pct(v: float) -> str:
    return f"{v:+.2f}%"


def main() -> int:
    cfg = load_config()
    ex = SolBBExecutor(cfg)

    bal = ex.client.get_balance()
    ticker = ex.client.get_ticker(cfg.instId)
    exchange_pos = ex.client.get_positions(cfg.instId)
    local_pos = ex.load_position()

    print("## 🟣 OKX SOL BB")
    print()

    try:
        r = subprocess.run(["systemctl", "is-active", "okx-sol-bb-monitor.service"],
                           capture_output=True, text=True, timeout=5)
        mon = r.stdout.strip()
    except Exception:
        mon = "unknown"
    print(f"**Monitor**: {'🟢 active' if mon == 'active' else f'⚠️ {mon}'}")
    print(f"**账户**: ${bal['total_equity']:.2f}")

    if ticker:
        print(f"**SOL**: ${ticker['last']:.2f}")

    # BB levels
    candles = ex.fetch_candles(limit=300)
    if len(candles) >= 30:
        from okx_sol_bb.strategy import get_bb_levels
        closes = ex.get_closes(candles)
        bb = get_bb_levels(closes, cfg.strategy.bb_period, cfg.strategy.bb_multiplier, len(closes) - 1)
        if bb:
            mid, upper, lower = bb
            price = closes[-1]
            dist_upper = (upper - price) / price * 100
            dist_lower = (price - lower) / price * 100
            print(f"**BB({cfg.strategy.bb_period},{cfg.strategy.bb_multiplier})**: "
                  f"{lower:.2f} - {mid:.2f} - {upper:.2f}")
            print(f"**距离**: 上轨 {dist_upper:+.1f}% | 下轨 -{dist_lower:.1f}%")

    print()

    if exchange_pos is None:
        print("**状态**: ⚠️ API 查询失败")
        return 0

    has_exchange = bool(exchange_pos and float(exchange_pos[0].get('pos', 0)) != 0)
    has_local = bool(local_pos)

    if has_exchange:
        p = exchange_pos[0]
        size = abs(float(p.get('pos', 0)))
        avg_px = float(p.get('avgPx', 0) or 0)
        upl = float(p.get('upl', 0) or 0)
        upl_ratio = float(p.get('uplRatio', 0) or 0) * 100
        direction = 'LONG' if float(p.get('pos', 0)) > 0 else 'SHORT'

        print("**当前持仓**:")
        print(f"- {cfg.coin} {direction} {size:g}张 @ ${avg_px:.2f} | 未实现: ${upl:+.2f} ({fmt_pct(upl_ratio)})")
        if local_pos:
            sl = local_pos.get('sl_price')
            tp = local_pos.get('tp_price')
            if sl is not None or tp is not None:
                sl_text = f"${float(sl):.2f}" if sl is not None else "None"
                tp_text = f"${float(tp):.2f}" if tp is not None else "None"
                print(f"- SL: {sl_text} | TP: {tp_text}")
            print(f"- 入场: {local_pos.get('entry_time', 'unknown')}")
            print("- ✅ 一致")
        else:
            print("- 🚨 交易所有仓位，本地无记录")
        return 0

    print("**当前持仓**: 无")
    if has_local:
        print("- 🚨 本地有记录，交易所已空")
    else:
        print("- ✅ 空仓")

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
