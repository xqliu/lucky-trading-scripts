#!/usr/bin/env python3
"""OKX BB Status — cross-verified between local state and exchange.

Reports both local and exchange position state.
If they disagree, flags it explicitly so cron/reports never misinterpret.
"""
import json
import logging
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from okx_bb.config import load_config
from okx_bb.executor import BBExecutor

logging.basicConfig(level=logging.WARNING)


def main():
    cfg = load_config()
    executor = BBExecutor(cfg)

    # Account balance (exchange API)
    balance = executor.client.get_balance()
    print(f"💰 账户: ${balance['total_equity']:.2f} | 可用: ${balance['usdt_available']:.2f}")

    # Ticker
    ticker = executor.client.get_ticker(cfg.instId)
    if ticker:
        print(f"📈 {cfg.coin}: ${ticker['last']:,.2f}")

    # --- Cross-verified position status ---
    local_pos = executor.load_position()
    exchange_pos = executor.client.get_positions(cfg.instId)

    # None = API error (can't determine); [] = confirmed no position
    if exchange_pos is None:
        print("⚠️ 交易所 API 查询失败，无法交叉验证")
        status = executor.position_status(local_pos)
        if status == "No position":
            print("📭 本地无持仓 (交易所未确认)")
        else:
            print(status)
            print("  ⚠️ 交易所侧未验证（API 超时）")
        # Monitor and exit early
        try:
            r = subprocess.run(["systemctl", "is-active", "okx-bb-monitor.service"],
                               capture_output=True, text=True, timeout=5)
            s = r.stdout.strip()
            print(f"{'🟢' if s == 'active' else '🔴'} Monitor: {s}")
        except Exception:
            print("⚠️ Monitor: 状态未知")
        return

    has_local = bool(local_pos)
    has_exchange = bool(len(exchange_pos) > 0 and float(exchange_pos[0].get("pos", 0)) != 0)

    if has_local and has_exchange:
        # Both agree there's a position — show details
        status = executor.position_status(local_pos)
        print(status)
        print("  ✅ 本地与交易所一致")

        # Also verify SL/TP are live
        algos = executor.client.get_algo_orders(cfg.instId, "conditional")
        orders = executor.client.get_open_orders(cfg.instId)
        sl_live = any(a.get("algoId") == local_pos.get("sl_algo_id") for a in (algos or []))
        tp_live = any(o.get("ordId") == local_pos.get("tp_order_id") for o in (orders or []))
        if not sl_live:
            print("  ⚠️ SL 挂单不在交易所！需要重新设置")
        if not tp_live:
            print("  ⚠️ TP 挂单不在交易所！需要重新设置")

    elif not has_local and not has_exchange:
        # Both agree no position
        print("📭 无持仓 (本地+交易所均确认)")

    elif has_exchange and not has_local:
        # Exchange has position but local doesn't — orphan
        ep = exchange_pos[0]
        pos_val = float(ep.get("pos", 0))
        direction = "LONG" if pos_val > 0 else "SHORT"
        print(f"🚨 状态不一致: 交易所有仓位但本地无记录!")
        print(f"  交易所: {direction} {abs(pos_val)} @ ${ep.get('avgPx')} | uPnL ${ep.get('upl')}")
        print(f"  本地: 无记录")
        print(f"  → 需要 reconcile 同步")

    elif has_local and not has_exchange:
        # Local has position but exchange doesn't — stale local state
        print(f"🚨 状态不一致: 本地记录有仓位但交易所已平仓!")
        print(f"  本地: {local_pos['direction']} @ ${local_pos['entry_price']} (入场 {local_pos.get('entry_time', '?')})")
        print(f"  交易所: 无持仓")
        print(f"  → 本地状态过期，需要清理")

    # Monitor service
    try:
        r = subprocess.run(["systemctl", "is-active", "okx-bb-monitor.service"],
                           capture_output=True, text=True, timeout=5)
        s = r.stdout.strip()
        print(f"{'🟢' if s == 'active' else '🔴'} Monitor: {s}")
    except Exception:
        print("⚠️ Monitor: 状态未知")


if __name__ == "__main__":
    main()
