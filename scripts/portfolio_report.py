#!/usr/bin/env python3
"""
Portfolio Report — single source of truth for all 3 strategies.

Usage:
    cd ~/.openclaw/workspace/trading/repos/lucky-trading-scripts
    source ~/.openclaw/workspace/trading/.venv/bin/activate
    LUCKYTRADER_CONFIG_DIR=~/.openclaw/workspace/trading/config \
    OKX_BB_CONFIG_DIR=~/.openclaw/workspace/trading/okx_bb/config \
    OKX_SOL_BB_CONFIG_DIR=~/.openclaw/workspace/trading/okx_sol_bb/config \
    python scripts/portfolio_report.py

Data sources (never trade_log.json):
  - HL: hyperliquid.info.user_fills() + user_state()
  - OKX: /api/v5/account/bills-archive (subType=1 for PnL, subType=2 for fees)
  - All numbers directly from exchange APIs

Bug trade identification:
  - OKX ETH: bills with abs(sz) > 1.0 are bug-caused (normal max is 0.42)
  - Can be extended with explicit bill IDs
"""

import os
import sys
import hmac
import hashlib
import base64
import json
import tomllib
import time
import requests
from datetime import datetime, timezone, timedelta
from collections import defaultdict

# === Constants ===
HL_EXPERIMENT_START = datetime(2026, 2, 1, tzinfo=timezone.utc)
HL_INITIAL_CAPITAL = 100.0
OKX_ETH_INITIAL_CAPITAL = 100.0
OKX_SOL_INITIAL_CAPITAL = 100.0

# Known bug bill timestamps (OKX ETH) — bills where position flipped due to bugs
# 03-02 15:09 buy 2.54 @ 2006.02 (flip recovery) and 03-15 22:36 buy 0.82 @ 2149.46 (flip recovery)
OKX_ETH_BUG_BILL_TIMESTAMPS = {
    1740927000000,  # approximate — will match by size instead
}
OKX_ETH_BUG_SIZE_THRESHOLD = 0.50  # normal trades ≤ 0.42 contracts


def _hl_report():
    """HL strategy report from Hyperliquid API."""
    config_dir = os.environ.get(
        "LUCKYTRADER_CONFIG_DIR",
        os.path.expanduser("~/.openclaw/workspace/trading/config"),
    )
    with open(os.path.join(config_dir, "config.toml"), "rb") as f:
        cfg = tomllib.load(f)
    address = cfg["exchange"]["main_wallet"]

    from hyperliquid.info import Info

    info = Info(skip_ws=True)
    state = info.user_state(address)
    fills = info.user_fills(address)
    equity = float(state.get("marginSummary", {}).get("accountValue", 0))

    # Filter experiment period
    exp_fills = [
        f
        for f in fills
        if datetime.fromtimestamp(f["time"] / 1000, tz=timezone.utc)
        >= HL_EXPERIMENT_START
    ]

    # By coin
    coins = defaultdict(list)
    for f in exp_fills:
        coins[f.get("coin", "?")].append(f)

    total_pnl = sum(float(f.get("closedPnl", 0)) for f in exp_fills)
    total_fee = sum(float(f.get("fee", 0)) for f in exp_fills)

    # Token income = equity - initial - trading_net
    trading_net = total_pnl - total_fee
    token_income = equity - HL_INITIAL_CAPITAL - trading_net

    # Weekly breakdown
    weeks = defaultdict(lambda: {"pnl": 0, "fee": 0, "fills": 0})
    for f in exp_fills:
        dt = datetime.fromtimestamp(f["time"] / 1000, tz=timezone.utc)
        wk = dt.strftime("%Y-W%W")
        weeks[wk]["pnl"] += float(f.get("closedPnl", 0))
        weeks[wk]["fee"] += float(f.get("fee", 0))
        weeks[wk]["fills"] += 1

    # Positions
    positions = []
    for p in state.get("assetPositions", []):
        pos = p["position"]
        szi = float(pos["szi"])
        if szi == 0:
            continue
        positions.append(
            {
                "coin": pos["coin"],
                "direction": "LONG" if szi > 0 else "SHORT",
                "size": abs(szi),
                "entry": float(pos["entryPx"]),
                "upnl": float(pos["unrealizedPnl"]),
                "leverage": pos.get("leverage", {}).get("value", "?"),
            }
        )

    print("=" * 60)
    print("📊 HL Momentum (BTC + ETH)")
    print(f"   实验期: 2026-02-01 ~ 至今")
    print("=" * 60)
    print(f"本金: ${HL_INITIAL_CAPITAL:.0f}")
    print(f"当前权益: ${equity:.2f}")
    print(f"纯交易 PnL: ${trading_net:.2f} ({trading_net/HL_INITIAL_CAPITAL*100:+.1f}%)")
    print(f"  closedPnl: ${total_pnl:.2f} | fees: ${total_fee:.2f}")
    print(f"代币收入: ~${token_income:.2f}")
    print(f"Fills: {len(exp_fills)}")
    print()

    for c in sorted(coins):
        fs = coins[c]
        cpnl = sum(float(ff.get("closedPnl", 0)) for ff in fs)
        cfee = sum(float(ff.get("fee", 0)) for ff in fs)
        wins = len([ff for ff in fs if float(ff.get("closedPnl", 0)) > 0.001])
        losses = len([ff for ff in fs if float(ff.get("closedPnl", 0)) < -0.001])
        wr = wins / (wins + losses) * 100 if (wins + losses) > 0 else 0
        print(
            f"  {c}: {len(fs)} fills | pnl=${cpnl:.2f} | fee=${cfee:.2f} | net=${cpnl-cfee:.2f} | WR={wr:.0f}%"
        )

    print("\n  周度:")
    win_weeks = 0
    for wk in sorted(weeks):
        w = weeks[wk]
        net = w["pnl"] - w["fee"]
        if net > 0:
            win_weeks += 1
        emoji = "🟢" if net > 0 else "🔴"
        print(f"    {emoji} {wk}: {w['fills']}笔 | net=${net:.2f}")
    print(f"  盈利周: {win_weeks}/{len(weeks)}")

    if positions:
        print("\n  持仓:")
        for p in positions:
            print(
                f"    {p['coin']} {p['direction']} {p['size']} @ ${p['entry']:.2f} | uPnL=${p['upnl']:.2f} | {p['leverage']}x"
            )
    else:
        print("\n  持仓: 无")

    return equity


def _okx_get_bills(client, instId, sub_type=None):
    """Paginated fetch of all bills from OKX bills-archive API."""
    all_bills = []
    before = None
    for _ in range(20):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        path = f"/api/v5/account/bills-archive?instType=SWAP&instId={instId}&limit=100"
        if sub_type:
            path += f"&subType={sub_type}"
        if before:
            path += f"&before={before}"

        message = ts + "GET" + path
        mac = hmac.new(
            client.secret_key.encode(), message.encode(), hashlib.sha256
        )
        sign = base64.b64encode(mac.digest()).decode()

        headers = {
            "OK-ACCESS-KEY": client.api_key,
            "OK-ACCESS-SIGN": sign,
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": client.passphrase,
        }

        resp = requests.get(f"https://www.okx.com{path}", headers=headers)
        data = resp.json()
        if data.get("code") != "0":
            print(f"  ⚠️ OKX API error: {data.get('msg')}")
            break

        bills = data.get("data", [])
        if not bills:
            break
        all_bills.extend(bills)
        before = bills[-1].get("billId")
        if len(bills) < 100:
            break
        time.sleep(0.2)

    return all_bills


def _okx_report(label, config_env, executor_cls, config_cls, instId, initial_capital, bug_size_threshold=None):
    """OKX strategy report from bills-archive API."""
    from okx_bb.exchange import OKXClient

    # Instantiate executor to get authenticated client
    ex = executor_cls()
    client = ex.client
    bal = client.get_balance()
    equity = float(bal.get("total_equity", 0))

    # Get realized PnL bills (subType=1)
    pnl_bills = _okx_get_bills(client, instId, sub_type="1")
    # Get fee bills (subType=2)
    fee_bills = _okx_get_bills(client, instId, sub_type="2")
    # Get funding bills (subType=173 + 174)
    fund_173 = _okx_get_bills(client, instId, sub_type="173")
    fund_174 = _okx_get_bills(client, instId, sub_type="174")

    # Calculate totals
    total_realized = sum(float(b.get("pnl", 0) or 0) for b in pnl_bills)
    total_fees = sum(float(b.get("pnl", 0) or 0) for b in fee_bills)  # OKX stores fee impact in pnl field
    total_funding = sum(float(b.get("pnl", 0) or 0) for b in fund_173) + sum(
        float(b.get("pnl", 0) or 0) for b in fund_174
    )

    # Split bug vs strategy (by size threshold)
    bug_pnl = 0
    strat_pnl = 0
    bug_count = 0
    strat_count = 0
    for b in pnl_bills:
        pnl = float(b.get("pnl", 0) or 0)
        sz = float(b.get("sz", 0) or 0)
        if bug_size_threshold and sz > bug_size_threshold:
            bug_pnl += pnl
            bug_count += 1
        else:
            strat_pnl += pnl
            strat_count += 1

    # Current position
    positions = client.get_positions(instId)
    upl = 0
    pos_info = None
    for p in positions:
        if float(p.get("pos", 0)) != 0:
            upl = float(p.get("upl", 0))
            d = "LONG" if float(p["pos"]) > 0 else "SHORT"
            pos_info = f"{d} {p['pos']} @ ${float(p['avgPx']):.2f} | uPnL=${upl:.2f}"

    # Net = realized + fees (negative) + funding + unrealized
    net_realized = total_realized + total_fees + total_funding
    expected_equity = initial_capital + net_realized + upl

    print()
    print("=" * 60)
    print(f"📊 {label}")
    print("=" * 60)
    print(f"本金: ${initial_capital:.0f}")
    print(f"当前权益: ${equity:.2f} ({(equity-initial_capital)/initial_capital*100:+.1f}%)")
    print()
    print(f"已实现交易 PnL: ${total_realized:+.4f} ({len(pnl_bills)} bills)")
    if bug_size_threshold:
        print(f"  策略: ${strat_pnl:+.4f} ({strat_count} bills)")
        print(f"  Bug:  ${bug_pnl:+.4f} ({bug_count} bills)")
    print(f"手续费: ${total_fees:+.4f} ({len(fee_bills)} bills)")
    print(f"资金费: ${total_funding:+.4f} ({len(fund_173)+len(fund_174)} bills)")
    print(f"当前浮盈: ${upl:+.2f}")
    print()
    print(f"已实现合计: ${net_realized:+.2f}")
    print(f"预期权益: ${expected_equity:.2f} (实际: ${equity:.2f}, 差=${equity-expected_equity:.2f})")

    if pos_info:
        print(f"\n持仓: {pos_info}")
    else:
        print(f"\n持仓: 无")

    # Trade-by-trade (pnl bills only, most recent first)
    if pnl_bills:
        print(f"\n交易明细 (subType=1 bills):")
        for b in pnl_bills:
            ts_ms = int(b.get("ts", 0))
            dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime(
                "%m-%d %H:%M"
            )
            pnl = float(b.get("pnl", 0) or 0)
            sz = float(b.get("sz", 0) or 0)
            px = b.get("px", "?")
            bug_flag = " 🔴BUG" if bug_size_threshold and sz > bug_size_threshold else ""
            print(f"  {dt} | sz={sz:.2f} @ ${px} | pnl=${pnl:+.4f}{bug_flag}")

    return equity


def main():
    print("=" * 60)
    print("📋 Portfolio Report")
    print(f"   Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 60)

    # 1. HL
    try:
        hl_equity = _hl_report()
    except Exception as e:
        print(f"\n⚠️ HL report failed: {e}")
        hl_equity = 0

    # 2. OKX ETH BB
    try:
        os.environ["OKX_BB_CONFIG_DIR"] = os.environ.get(
            "OKX_BB_CONFIG_DIR",
            os.path.expanduser("~/.openclaw/workspace/trading/okx_bb/config"),
        )
        from okx_bb.executor import BBExecutor

        eth_equity = _okx_report(
            "OKX ETH BB (Breakout)",
            "OKX_BB_CONFIG_DIR",
            BBExecutor,
            None,
            "ETH-USDT-SWAP",
            OKX_ETH_INITIAL_CAPITAL,
            bug_size_threshold=OKX_ETH_BUG_SIZE_THRESHOLD,
        )
    except Exception as e:
        print(f"\n⚠️ OKX ETH report failed: {e}")
        import traceback; traceback.print_exc()
        eth_equity = 0

    # 3. OKX SOL BB
    try:
        os.environ["OKX_SOL_BB_CONFIG_DIR"] = os.environ.get(
            "OKX_SOL_BB_CONFIG_DIR",
            os.path.expanduser("~/.openclaw/workspace/trading/okx_sol_bb/config"),
        )
        from okx_sol_bb.executor import SolBBExecutor

        sol_equity = _okx_report(
            "OKX SOL BB (Mean Reversion)",
            "OKX_SOL_BB_CONFIG_DIR",
            SolBBExecutor,
            None,
            "SOL-USDT-SWAP",
            OKX_SOL_INITIAL_CAPITAL,
            bug_size_threshold=None,  # SOL hasn't had bugs yet
        )
    except Exception as e:
        print(f"\n⚠️ OKX SOL report failed: {e}")
        import traceback; traceback.print_exc()
        sol_equity = 0

    # Summary
    total = hl_equity + eth_equity + sol_equity
    total_capital = HL_INITIAL_CAPITAL + OKX_ETH_INITIAL_CAPITAL + OKX_SOL_INITIAL_CAPITAL
    print()
    print("=" * 60)
    print("📋 汇总")
    print("=" * 60)
    print(f"总资产: ${total:.2f}")
    print(f"总本金: ${total_capital:.0f}")
    print(f"总回报: ${total - total_capital:.2f} ({(total-total_capital)/total_capital*100:+.1f}%)")
    print(f"  HL:  ${hl_equity:.2f}")
    print(f"  ETH: ${eth_equity:.2f}")
    print(f"  SOL: ${sol_equity:.2f}")


if __name__ == "__main__":
    main()
