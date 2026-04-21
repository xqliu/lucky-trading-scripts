#!/usr/bin/env python3
"""Daily recap for OKX SOL BB using current runtime APIs.

Replaces legacy one-liner snippets that imported okx_sol_bb.exchange directly.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault(
    "OKX_SOL_BB_CONFIG_DIR",
    str(Path.home() / ".openclaw/workspace/trading/okx_sol_bb/config"),
)

from okx_sol_bb.config import load_config
from okx_sol_bb.executor import SolBBExecutor


def main() -> int:
    cfg = load_config()
    ex = SolBBExecutor(cfg)
    bal = ex.client.get_balance()
    exchange_pos = ex.client.get_positions(cfg.instId)
    local_pos = ex.load_position()

    print("=== OKX SOL BB Daily Recap ===")
    print(f"Account equity: ${bal['total_equity']:.2f}")
    print(f"USDT available: ${bal['usdt_available']:.2f}")

    if exchange_pos is None:
        print("Position check failed: private API unavailable")
        return 1

    live = next((p for p in exchange_pos if float(p.get('pos', 0)) != 0), None)
    if not live:
        print("Position: none")
        return 0

    direction = "LONG" if float(live.get("pos", 0)) > 0 else "SHORT"
    avg_px = float(live.get("avgPx", 0) or 0)
    size = abs(float(live.get("pos", 0) or 0))
    upl = float(live.get("upl", 0) or 0)
    upl_ratio = float(live.get("uplRatio", 0) or 0) * 100
    print(f"Position: {direction} {size:g} @ ${avg_px:.2f} | uPnL ${upl:+.2f} ({upl_ratio:+.2f}%)")

    if local_pos:
        sl = local_pos.get("sl_price")
        tp = local_pos.get("tp_price")
        if sl is not None or tp is not None:
            sl_text = f"${float(sl):.2f}" if sl is not None else "None"
            tp_text = f"${float(tp):.2f}" if tp is not None else "None"
            print(f"SL: {sl_text} | TP: {tp_text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
