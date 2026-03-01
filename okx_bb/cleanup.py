#!/usr/bin/env python3
"""Cancel pending TRIGGER orders on shutdown.
NEVER cancel SL/TP when there's an open position — that would leave a naked position."""
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault('OKX_BB_CONFIG_DIR', str(Path(__file__).parent / 'config'))

from okx_bb.exchange import OKXClient
from okx_bb.config import load_config

def cleanup():
    cfg = load_config()
    client = OKXClient(cfg.api_key, cfg.secret_key, cfg.passphrase)
    total = 0

    # Check if there's an open position
    positions = client.get_positions(cfg.instId)
    if positions is None:
        print("⚠️ Cannot check positions (API error) — aborting cleanup to be safe")
        return
    has_position = any(float(p.get('pos', 0)) != 0 for p in positions)

    if has_position:
        print("⚠️ Open position detected — keeping SL/TP, only cancelling triggers")

    # Always cancel trigger orders (entry triggers)
    try:
        algos = client.get_algo_orders(instId=cfg.instId, ordType="trigger")
        for a in algos:
            try:
                client.cancel_algo_order(a['algoId'], cfg.instId)
                print(f"Cancelled trigger {a['algoId']}")
                total += 1
            except Exception as e:
                print(f"Failed to cancel {a['algoId']}: {e}")
    except Exception as e:
        print(f"Failed to list trigger orders: {e}")

    # Only cancel SL/TP if NO open position
    if not has_position:
        for ord_type in ["conditional"]:
            try:
                algos = client.get_algo_orders(instId=cfg.instId, ordType=ord_type)
                for a in algos:
                    try:
                        client.cancel_algo_order(a['algoId'], cfg.instId)
                        print(f"Cancelled {ord_type} {a['algoId']}")
                        total += 1
                    except Exception as e:
                        print(f"Failed to cancel {a['algoId']}: {e}")
            except Exception as e:
                print(f"Failed to list {ord_type} orders: {e}")

        try:
            orders = client.get_open_orders(instId=cfg.instId)
            for o in orders:
                try:
                    client.cancel_order(cfg.instId, o['ordId'])
                    print(f"Cancelled order {o['ordId']}")
                    total += 1
                except Exception as e:
                    print(f"Failed to cancel {o['ordId']}: {e}")
        except Exception as e:
            print(f"Failed to list orders: {e}")

    print(f"Cleanup done: {total} orders cancelled (position={'YES' if has_position else 'NO'})")

if __name__ == "__main__":
    cleanup()
