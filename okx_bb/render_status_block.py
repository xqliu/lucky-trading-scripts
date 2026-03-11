#!/usr/bin/env python3
"""Render OKX BB status block for reports.

Single source of truth for market-report OKX section.
No natural-language inference by cron AI.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from okx_bb.config import load_config
from okx_bb.executor import BBExecutor


def fmt_pct(v: float) -> str:
    return f"{v:+.2f}%"


def main() -> int:
    cfg = load_config()
    ex = BBExecutor(cfg)

    bal = ex.client.get_balance()
    ticker = ex.client.get_ticker(cfg.instId)
    exchange_pos = ex.client.get_positions(cfg.instId)
    local_pos = ex.load_position()

    print("## 🟠 OKX BB")
    print()

    try:
        r = subprocess.run(["systemctl", "is-active", "okx-bb-monitor.service"], capture_output=True, text=True, timeout=5)
        mon = r.stdout.strip()
    except Exception:
        mon = "unknown"
    print(f"**Monitor**: {'🟢 active' if mon == 'active' else f'⚠️ {mon}'}")
    print(f"**账户**: ${bal['total_equity']:.2f}")
    print()

    if exchange_pos is None:
        print("**状态**: ⚠️ 交易所 API 查询失败，无法确认持仓")
        if local_pos:
            print(f"**本地记录**: {local_pos['direction']} {cfg.coin} @ ${float(local_pos['entry_price']):.2f}")
        else:
            print("**本地记录**: 无持仓")
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
            print("- 状态校验: ✅ 本地与交易所一致")
        else:
            print("- 状态校验: 🚨 交易所有仓位，但本地无记录")
        return 0

    # no exchange position
    print("**当前持仓**: 无")
    if has_local:
        print("- 状态校验: 🚨 本地记录有仓位，但交易所已空仓")
        print(f"- 本地残留: {local_pos['direction']} {cfg.coin} @ ${float(local_pos['entry_price']):.2f}")
    else:
        print("- 状态校验: ✅ 本地与交易所均确认空仓")

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
