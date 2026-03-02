#!/usr/bin/env python3
"""
OKX BB Canonical Backtest
=========================
Uses EXACTLY the same functions as production:
  - get_bb_levels() from okx_bb.strategy
  - ema() from core.indicators (same as ws_monitor._get_trend)
  - Config from okx_bb.config

Two modes:
  1. CLOSE: signal when close breaks BB (matches detect_signal)
  2. INTRABAR: signal when high/low touches BB (matches ws_monitor trigger orders)

Entry price = BB boundary (trigger order fill simulation).
SL/TP/timeout/fees all from config.
"""
import sys
import json
import math
from pathlib import Path
from dataclasses import dataclass
from typing import List, Optional, Tuple

# Ensure imports work
_parent = str(Path(__file__).parent.parent)
if _parent not in sys.path:
    sys.path.insert(0, _parent)

from okx_bb.strategy import detect_signal, get_bb_levels
from okx_bb.config import load_config, OKXConfig
from core.indicators import ema


# === Trend detection — identical to ws_monitor._get_trend ===

def get_trend(closes: List[float], idx: int, period: int, lookback: int) -> Optional[str]:
    """Exact copy of ws_monitor._get_trend logic."""
    ema_start = max(0, idx - period * 3)
    ema_vals = ema(closes[ema_start:idx + 1], period)
    if len(ema_vals) < lookback + 1:
        return None
    if ema_vals[-1] > ema_vals[-1 - lookback]:
        return "up"
    elif ema_vals[-1] < ema_vals[-1 - lookback]:
        return "down"
    return None


# === Trade simulation ===

@dataclass
class Trade:
    entry_idx: int
    exit_idx: int
    direction: str
    entry_price: float
    exit_price: float
    pnl: float  # net of fees
    reason: str  # "sl" | "tp" | "timeout"


def simulate_trade(candles: list, entry_idx: int, direction: str,
                   entry_price: float, sl_pct: float, tp_pct: float,
                   max_hold: int, fee: float,
                   check_entry_bar: bool = False) -> Trade:
    """Simulate a single trade with SL/TP/timeout.

    Uses OHLC bars to check SL/TP intrabar (conservative: SL checked first).

    Args:
        check_entry_bar: If True, check entry bar's H/L for SL/TP hit.
            Use for intrabar entries where trigger fires mid-bar.
    """
    if direction == "LONG":
        sl = entry_price * (1 - sl_pct)
        tp = entry_price * (1 + tp_pct)
    else:
        sl = entry_price * (1 + sl_pct)
        tp = entry_price * (1 - tp_pct)

    start_idx = entry_idx if check_entry_bar else entry_idx + 1
    for i in range(start_idx, min(entry_idx + max_hold + 1, len(candles))):
        bar = candles[i]
        if direction == "LONG":
            # Check SL first (conservative)
            if bar["l"] <= sl:
                exit_price = sl
                pnl = (exit_price - entry_price) / entry_price - fee
                return Trade(entry_idx, i, direction, entry_price, exit_price, pnl, "sl")
            if bar["h"] >= tp:
                exit_price = tp
                pnl = (exit_price - entry_price) / entry_price - fee
                return Trade(entry_idx, i, direction, entry_price, exit_price, pnl, "tp")
        else:
            # SHORT: SL first
            if bar["h"] >= sl:
                exit_price = sl
                pnl = (entry_price - exit_price) / entry_price - fee
                return Trade(entry_idx, i, direction, entry_price, exit_price, pnl, "sl")
            if bar["l"] <= tp:
                exit_price = tp
                pnl = (entry_price - exit_price) / entry_price - fee
                return Trade(entry_idx, i, direction, entry_price, exit_price, pnl, "tp")

    # Timeout — exit at close of last bar
    exit_idx = min(entry_idx + max_hold, len(candles) - 1)
    exit_price = candles[exit_idx]["c"]
    if direction == "LONG":
        pnl = (exit_price - entry_price) / entry_price - fee
    else:
        pnl = (entry_price - exit_price) / entry_price - fee
    return Trade(entry_idx, exit_idx, direction, entry_price, exit_price, pnl, "timeout")


# === Backtest engines ===

def backtest_close(candles: list, cfg: OKXConfig) -> List[Trade]:
    """Close-only mode: uses detect_signal() directly.

    Entry at next bar open (simulating market order after close signal).
    """
    closes = [c["c"] for c in candles]
    trades = []
    in_trade = False
    exit_idx = 0
    fee = (cfg.fees.taker_fee + cfg.fees.taker_fee)  # taker both sides

    min_bars = max(cfg.strategy.bb_period + 1,
                   cfg.strategy.trend_ema_period + cfg.strategy.trend_lookback + 1)

    for idx in range(min_bars, len(candles) - 1):
        if in_trade and idx <= exit_idx:
            continue
        in_trade = False

        signal = detect_signal(closes, cfg.strategy.bb_period, cfg.strategy.bb_multiplier,
                               cfg.strategy.trend_ema_period, cfg.strategy.trend_lookback, idx)
        if signal:
            entry_price = candles[idx + 1]["o"]  # next bar open
            trade = simulate_trade(candles, idx + 1, signal, entry_price,
                                   cfg.risk.stop_loss_pct, cfg.risk.take_profit_pct,
                                   cfg.risk.max_hold_bars, fee)
            trades.append(trade)
            in_trade = True
            exit_idx = trade.exit_idx

    return trades


def backtest_intrabar(candles: list, cfg: OKXConfig) -> List[Trade]:
    """Intrabar mode: matches ws_monitor trigger order logic exactly.

    On each bar, compute BB + trend (using get_bb_levels + get_trend).
    If high >= upper and trend=up → LONG at upper (trigger fill).
    If low <= lower and trend=down → SHORT at lower (trigger fill).

    This mirrors: ws_monitor places trigger at BB boundary, OKX fires when
    price touches it, fill at trigger price (market order, ~0 slippage on ETH).
    """
    closes = [c["c"] for c in candles]
    trades = []
    in_trade = False
    exit_idx = 0
    fee = (cfg.fees.taker_fee + cfg.fees.taker_fee)

    min_bars = max(cfg.strategy.bb_period + 1,
                   cfg.strategy.trend_ema_period + cfg.strategy.trend_lookback + 1)

    for idx in range(min_bars, len(candles)):
        if in_trade and idx <= exit_idx:
            continue
        in_trade = False

        # Same as ws_monitor._place_triggers:
        bb = get_bb_levels(closes, cfg.strategy.bb_period, cfg.strategy.bb_multiplier, idx)
        if bb is None:
            continue
        _, upper, lower = bb

        trend = get_trend(closes, idx, cfg.strategy.trend_ema_period,
                          cfg.strategy.trend_lookback)

        bar = candles[idx]

        signal = None
        entry_price = None

        if trend == "up" and bar["h"] >= upper:
            signal = "LONG"
            entry_price = upper  # trigger fires at BB boundary, fills at ~triggerPx
        elif trend == "down" and bar["l"] <= lower:
            signal = "SHORT"
            entry_price = lower  # orderPx buffer is just fill ceiling, not actual price

        if signal and entry_price:
            trade = simulate_trade(candles, idx, signal, entry_price,
                                   cfg.risk.stop_loss_pct, cfg.risk.take_profit_pct,
                                   cfg.risk.max_hold_bars, fee,
                                   check_entry_bar=True)
            trades.append(trade)
            in_trade = True
            exit_idx = trade.exit_idx

    return trades


# === Reporting ===

def report(name: str, trades: List[Trade], candles: list, leverage: int = 1,
           position_ratio: float = 1.0):
    """Print backtest statistics.

    Args:
        leverage: Position leverage multiplier.
        position_ratio: Fraction of equity used per trade (0-1).
            Effective leverage = leverage * position_ratio.
            PnL per trade scaled by effective leverage.
    """
    if not trades:
        print(f"\n{'='*50}")
        print(f"  {name}: 0 trades")
        return

    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    win_rate = len(wins) / len(trades) * 100

    # Equity curve (compounded) — apply effective leverage to each trade's PnL
    # Effective leverage = leverage * position_ratio
    # Clamp leveraged PnL to -100% (liquidation — can't lose more than account)
    eff_lev = leverage * position_ratio
    equity = [1.0]
    for t in trades:
        lev_pnl = t.pnl * eff_lev
        if lev_pnl < -1.0:
            lev_pnl = -1.0  # liquidated — account wiped
        equity.append(equity[-1] * (1 + lev_pnl))
        if equity[-1] < 1e-10:
            break  # account blown, stop
    compounded_return = equity[-1] - 1  # This is the REAL return

    peak = equity[0]
    max_dd = 0
    for e in equity:
        if e > peak:
            peak = e
        dd = (peak - e) / peak
        if dd > max_dd:
            max_dd = dd

    # Profit factor
    gross_win = sum(t.pnl for t in wins) if wins else 0
    gross_loss = abs(sum(t.pnl for t in losses)) if losses else 0.001
    pf = gross_win / gross_loss

    # Walk-forward (4 segments) — use compounded equity per segment WITH leverage
    seg_size = len(trades) // 4
    wf_pass = 0
    for i in range(4):
        seg = trades[i * seg_size:(i + 1) * seg_size] if i < 3 else trades[i * seg_size:]
        seg_eq = 1.0
        for t in seg:
            lp = t.pnl * eff_lev
            if lp < -1.0:
                lp = -1.0
            seg_eq *= (1 + lp)
            if seg_eq < 1e-10:
                break
        if seg_eq > 1.0:
            wf_pass += 1

    # Exit breakdown
    sl_count = sum(1 for t in trades if t.reason == "sl")
    tp_count = sum(1 for t in trades if t.reason == "tp")
    to_count = sum(1 for t in trades if t.reason == "timeout")

    # Time range
    days = (candles[-1]["ts"] - candles[0]["ts"]) / 86400000

    lev_label = f"{leverage}x" if position_ratio == 1.0 else f"{leverage}x × {position_ratio:.0%} = eff {eff_lev:.1f}x"
    print(f"\n{'='*60}")
    print(f"  {name}  [{lev_label}]")
    print(f"{'='*60}")
    print(f"  Period: {days:.0f} days | Trades: {len(trades)}")
    print(f"  Return: {compounded_return*100:+.1f}% (compounded)")
    print(f"  $100 → ${100 * equity[-1]:.2f}")
    print(f"  Win rate: {win_rate:.1f}% | PF: {pf:.2f}")
    print(f"  Max DD: {max_dd*100:.1f}%")
    print(f"  WF: {wf_pass}/4")
    print(f"  Exits: SL={sl_count} TP={tp_count} Timeout={to_count}")
    avg_w = sum(t.pnl for t in wins)/len(wins)*eff_lev*100 if wins else 0
    avg_l = sum(t.pnl for t in losses)/len(losses)*eff_lev*100 if losses else 0
    print(f"  Avg win: {avg_w:+.2f}% (account)" if wins else "  No wins")
    print(f"  Avg loss: {avg_l:+.2f}% (account)" if losses else "  No losses")
    print(f"{'='*60}")

    return {
        "name": name, "trades": len(trades),
        "return_pct": compounded_return,
        "win_rate": win_rate, "pf": pf, "mdd": max_dd,
        "wf": wf_pass, "final_equity": 100 * equity[-1],
    }


# === Data fetching ===

def fetch_candles(cfg: OKXConfig, max_candles: int = 50000) -> list:
    """Fetch historical candles from OKX API."""
    import time as _time
    from okx_bb.exchange import OKXClient

    client = OKXClient(cfg.api_key, cfg.secret_key, cfg.passphrase)
    all_candles = []
    after = ""

    while len(all_candles) < max_candles:
        params = {"instId": cfg.instId, "bar": "30m", "limit": "300"}
        if after:
            params["after"] = after

        data = client._request("GET", "/market/history-candles", params=params)
        if data.get("code") != "0" or not data.get("data"):
            if not all_candles:
                data = client._request("GET", "/market/candles", params=params)
                if data.get("code") != "0" or not data.get("data"):
                    break
            else:
                break

        rows = data["data"]
        if not rows:
            break

        for row in rows:
            all_candles.append({
                "ts": int(row[0]),
                "o": float(row[1]),
                "h": float(row[2]),
                "l": float(row[3]),
                "c": float(row[4]),
                "vol": float(row[5]),
            })

        after = rows[-1][0]
        _time.sleep(0.2)

        if len(rows) < 100:
            break

    # Sort oldest first, deduplicate
    all_candles.sort(key=lambda x: x["ts"])
    seen = set()
    unique = []
    for c in all_candles:
        if c["ts"] not in seen:
            seen.add(c["ts"])
            unique.append(c)

    return unique


def load_or_fetch_candles(cfg: OKXConfig, cache_dir: Optional[Path] = None) -> list:
    """Load candles from local cache, fetch new ones from API, merge and save.

    Cache file: {cache_dir}/eth_30m_candles.json
    On each run: load cache → fetch only newer candles → merge → save.
    """
    import json as _json

    if cache_dir is None:
        cache_dir = Path(__file__).parent / "data"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"{cfg.coin.lower()}_30m_candles.json"

    cached = []
    if cache_file.exists():
        with open(cache_file) as f:
            cached = _json.load(f)
        print(f"Cache: {len(cached)} candles "
              f"({(cached[-1]['ts'] - cached[0]['ts']) / 86400000:.0f} days)")

    # Fetch new candles from API (always, to get latest)
    fresh = fetch_candles(cfg, max_candles=50000)

    # Merge: deduplicate by timestamp
    by_ts = {c["ts"]: c for c in cached}
    for c in fresh:
        by_ts[c["ts"]] = c  # fresh overwrites cached
    merged = sorted(by_ts.values(), key=lambda x: x["ts"])

    # Save
    with open(cache_file, "w") as f:
        _json.dump(merged, f)

    new_count = len(merged) - len(cached)
    if new_count > 0:
        print(f"Added {new_count} new candles, total {len(merged)}")

    return merged


def main():
    import os
    # Use config from okx_bb/config/ directory
    config_dir = Path(__file__).parent / "config"
    if config_dir.exists():
        os.environ["OKX_BB_CONFIG_DIR"] = str(config_dir)

    cfg = load_config()

    print(f"Loading ETH-USDT-SWAP 30m candles...")
    candles = load_or_fetch_candles(cfg)
    print(f"Total: {len(candles)} candles "
          f"({(candles[-1]['ts'] - candles[0]['ts']) / 86400000:.0f} days)")

    print(f"\nConfig: BB({cfg.strategy.bb_period}, {cfg.strategy.bb_multiplier}) "
          f"EMA({cfg.strategy.trend_ema_period}, lookback={cfg.strategy.trend_lookback})")
    lev = cfg.risk.leverage
    pr = cfg.risk.position_ratio
    eff = lev * pr
    print(f"Risk: TP={cfg.risk.take_profit_pct*100}% SL={cfg.risk.stop_loss_pct*100}% "
          f"MaxHold={cfg.risk.max_hold_bars} bars")
    print(f"Leverage: {lev}x  Position ratio: {pr:.0%}  Effective: {eff:.1f}x")
    print(f"Fee: {cfg.fees.taker_fee*2*100:.2f}% round-trip")

    # Run both modes
    close_trades = backtest_close(candles, cfg)
    intrabar_trades = backtest_intrabar(candles, cfg)

    pr = cfg.risk.position_ratio
    r1 = report("CLOSE (detect_signal)", close_trades, candles, leverage=lev, position_ratio=pr)
    r2 = report("INTRABAR (ws_monitor trigger)", intrabar_trades, candles, leverage=lev, position_ratio=pr)

    if r1 and r2:
        print(f"\nIntrabar vs Close: ${r2['final_equity']:.0f} vs ${r1['final_equity']:.0f} "
              f"({r2['trades'] - r1['trades']:+d} trades)")


if __name__ == "__main__":
    main()
