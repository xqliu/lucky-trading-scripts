#!/usr/bin/env python3
"""Cancel ALL pending orders on shutdown (both trigger and conditional types)."""
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

    # Cancel BOTH trigger and conditional algo orders
    for ord_type in ["trigger", "conditional"]:
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

    # Cancel regular orders (TP limits)
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

    print(f"Cleanup done: {total} orders cancelled")

if __name__ == "__main__":
    cleanup()
