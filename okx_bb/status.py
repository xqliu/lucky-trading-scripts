#!/usr/bin/env python3
"""OKX BB Status — delegates ALL position/PnL reporting to BBExecutor.

No duplicated PnL calculation. Executor.position_status() is the single source of truth.
"""
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

    # Position status — single source of truth from executor
    status = executor.position_status()
    if status == "No position":
        print("📭 无持仓")
    else:
        print(status)

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
