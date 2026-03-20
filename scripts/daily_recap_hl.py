#!/usr/bin/env python3
"""Daily HL trade recap — outputs factual data only."""
import tomllib, json, os
from datetime import datetime, timezone
from hyperliquid.info import Info

CONFIG_DIR = os.environ.get("LUCKYTRADER_CONFIG_DIR", os.path.expanduser("~/.openclaw/workspace/trading/config"))
with open(os.path.join(CONFIG_DIR, "config.toml"), "rb") as f:
    cfg = tomllib.load(f)

address = cfg["exchange"]["main_wallet"]
info = Info(skip_ws=True)

# Fills today (UTC)
fills = info.user_fills(address)
today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
today_fills = [f for f in fills if datetime.fromtimestamp(f["time"]/1000, tz=timezone.utc).strftime("%Y-%m-%d") == today]

# Group by coin
coins = {}
for f in today_fills:
    c = f.get("coin", "?")
    coins.setdefault(c, []).append(f)

# Positions
state = info.user_state(address)
positions = [p for p in state.get("assetPositions", []) if float(p["position"]["szi"]) != 0]
equity = float(state.get("marginSummary", {}).get("accountValue", 0))

# Open orders
open_orders = info.open_orders(address)

print(f"=== HL Daily Recap ({today} UTC) ===")
print(f"Account equity: ${equity:.2f}")
print(f"Today fills: {len(today_fills)}")
print()

if today_fills:
    total_pnl = 0
    total_fee = 0
    for c, fs in sorted(coins.items()):
        pnl = sum(float(ff.get("closedPnl", 0)) for ff in fs)
        fee = sum(float(ff.get("fee", 0)) for ff in fs)
        total_pnl += pnl
        total_fee += fee
        print(f"  {c}: {len(fs)} fills | closedPnl=${pnl:.4f} | fee=${fee:.4f} | net=${pnl-fee:.4f}")
        for ff in fs:
            ts = datetime.fromtimestamp(ff["time"]/1000, tz=timezone.utc).strftime("%H:%M")
            side = ff.get("side", "?")
            sz = ff.get("sz", "?")
            px = ff.get("px", "?")
            cpnl = ff.get("closedPnl", "0")
            print(f"    {ts} UTC {side} {sz} @ ${px} | closedPnl=${cpnl}")
    print(f"\n  TOTAL: closedPnl=${total_pnl:.4f} | fee=${total_fee:.4f} | net=${total_pnl-total_fee:.4f}")
else:
    print("  No fills today")

print(f"\nPositions: {len(positions)}")
for p in positions:
    pos = p["position"]
    coin = pos["coin"]
    szi = float(pos["szi"])
    entry = float(pos["entryPx"])
    upnl = float(pos["unrealizedPnl"])
    lev = pos.get("leverage", {}).get("value", "?")
    direction = "LONG" if szi > 0 else "SHORT"
    print(f"  {coin} {direction} {abs(szi)} @ ${entry:.2f} | uPnL=${upnl:.2f} | {lev}x")

print(f"\nOpen orders: {len(open_orders)}")
for o in open_orders[:10]:
    coin = o.get("coin", "?")
    side = o.get("side", "?")
    sz = o.get("sz", "?")
    px = o.get("limitPx", "?")
    otype = o.get("orderType", "?")
    print(f"  {coin} {side} {sz} @ ${px} ({otype})")
