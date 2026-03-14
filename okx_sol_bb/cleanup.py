#!/usr/bin/env python3
"""Cancel pending orders on shutdown.
NEVER cancel SL/TP when there's an open position — that would leave a naked position."""
import os, sys
from pathlib import Path

_parent = str(Path(__file__).parent.parent)
if _parent not in sys.path:
    sys.path.insert(0, _parent)

from okx_sol_bb.config import load_config
from okx_bb.exchange import OKXClient

def main():
    cfg = load_config()
    client = OKXClient(cfg.api_key, cfg.secret_key, cfg.passphrase)

    positions = client.get_positions(cfg.instId)
    has_position = positions and any(float(p.get("pos", 0)) != 0 for p in positions)

    if has_position:
        print("Position open — NOT cancelling SL/TP orders")
        return

    # Only cancel trigger orders (entry triggers), not conditional (SL/TP)
    algos = client.get_algo_orders(cfg.instId, "trigger")
    for a in (algos or []):
        try:
            client.cancel_algo_order(a["algoId"], cfg.instId)
            print(f"Cancelled trigger {a['algoId']}")
        except Exception as e:
            print(f"Cancel failed: {e}")

if __name__ == "__main__":
    main()
