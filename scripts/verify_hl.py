#!/usr/bin/env python3
"""
Hyperliquid WS Monitor 交易逻辑验证器（只读，不下单）
=====================================================
从 HL API 拉实时数据，用生产代码计算信号，
对比 ws_monitor 实际行为，检查一致性。

用法: python scripts/verify_hl.py
输出: 人类可读的验证报告
"""
import sys
import json
import subprocess
from pathlib import Path
from datetime import datetime, timezone, timedelta

# Setup path
_root = str(Path(__file__).parent.parent)
if _root not in sys.path:
    sys.path.insert(0, _root)

from luckytrader.strategy import detect_signal as hl_detect_signal
from luckytrader.config import get_config
from luckytrader.indicators import ema
from hyperliquid.info import Info


def get_candles_from_hl(info: Info, coin: str, interval: str = "30m", lookback_hours: int = 72):
    """Fetch candles from Hyperliquid."""
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - lookback_hours * 3600 * 1000
    raw = info.candles_snapshot(coin, interval, start_ms, end_ms)
    candles = []
    for r in raw:
        candles.append({
            "t": r["t"],
            "T": r["T"],
            "o": float(r["o"]),
            "h": float(r["h"]),
            "l": float(r["l"]),
            "c": float(r["c"]),
            "v": float(r["v"]),
        })
    return candles


def get_4h_candles(info: Info, coin: str, lookback_hours: int = 336):
    """Fetch 4h candles for trend filter."""
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - lookback_hours * 3600 * 1000
    raw = info.candles_snapshot(coin, "4h", start_ms, end_ms)
    candles = []
    for r in raw:
        candles.append({
            "t": r["t"],
            "T": r["T"],
            "o": float(r["o"]),
            "h": float(r["h"]),
            "l": float(r["l"]),
            "c": float(r["c"]),
            "v": float(r["v"]),
        })
    return candles


def get_ws_monitor_log_last_signals(coin: str, n=10):
    """Parse recent signal-related lines from ws-monitor journal."""
    try:
        result = subprocess.run(
            ["journalctl", "-u", "ws-monitor", "--no-pager", "-n", "500"],
            capture_output=True, text=True, timeout=10
        )
        lines = result.stdout.strip().split("\n")
        sig_lines = [l for l in lines if coin in l and ("signal" in l.lower() or "HOLD" in l or "LONG" in l or "SHORT" in l or "detect" in l.lower())]
        return sig_lines[-n:] if len(sig_lines) >= n else sig_lines
    except Exception as e:
        return [f"(could not read journal: {e})"]


def get_hl_positions(info: Info, wallet: str):
    """Get current HL positions."""
    try:
        state = info.user_state(wallet)
        positions = []
        for p in state.get("assetPositions", []):
            pos = p.get("position", {})
            if float(pos.get("szi", 0)) != 0:
                positions.append({
                    "coin": pos.get("coin"),
                    "size": float(pos.get("szi", 0)),
                    "entry_px": float(pos.get("entryPx", 0)),
                    "unrealized_pnl": float(pos.get("unrealizedPnl", 0)),
                    "leverage": pos.get("leverage", {}),
                })
        return positions
    except Exception as e:
        return [{"error": str(e)}]


def verify_coin(info: Info, coin: str, cfg, report: list):
    """Verify one coin's trading logic. Compact output."""
    # 1. Fetch candles
    try:
        candles_30m = get_candles_from_hl(info, coin, "30m", 72)
        candles_4h = get_4h_candles(info, coin, 336)
    except Exception as e:
        report.append(f"❌ {coin}: 无法获取K线 {e}")
        return

    # 2. Signal
    try:
        idx = len(candles_30m) - 1
        coin_cfg = getattr(cfg.coins, coin, None) if hasattr(cfg, 'coins') else None
        signal = hl_detect_signal(candles_30m, candles_4h, idx, cfg, coin_cfg=coin_cfg)
    except Exception as e:
        report.append(f"❌ {coin}: 信号计算异常 {e}")
        return

    # 3. 4h trend
    trend_4h = "?"
    if candles_4h and len(candles_4h) > 21:
        closes_4h = [c["c"] for c in candles_4h]
        ema8 = ema(closes_4h, 8)
        ema21 = ema(closes_4h, 21)
        if ema8 and ema21:
            trend_4h = "UP" if ema8[-1] > ema21[-1] else "DOWN"

    # 4. Volume ratio
    vol_str = ""
    if candles_30m:
        vols = [c["v"] for c in candles_30m]
        coin_cfg2 = getattr(cfg.coins, coin, None) if hasattr(cfg, 'coins') else None
        lookback = getattr(coin_cfg2, 'lookback_bars', cfg.strategy.lookback_bars) if coin_cfg2 else cfg.strategy.lookback_bars
        if len(vols) > lookback:
            avg_vol = sum(vols[-lookback:]) / lookback
            vol_ratio = vols[-1] / avg_vol if avg_vol > 0 else 0
            vol_str = f"vol {vol_ratio:.2f}x"

    close_px = candles_30m[-1]['c'] if candles_30m else 0
    report.append(f"{coin} {close_px:.1f}｜{signal or 'HOLD'}｜4h {trend_4h}｜{vol_str}")


def main():
    report = []

    import os
    os.environ.setdefault("LUCKYTRADER_CONFIG_DIR",
                          str(Path.home() / ".openclaw/workspace/trading/config"))
    cfg = get_config()
    info = Info(skip_ws=True)

    # Positions + account
    wallet = cfg.exchange.main_wallet
    positions = get_hl_positions(info, wallet)
    pos_parts = []
    if positions and "error" not in positions[0]:
        for p in positions:
            pos_parts.append(f"{p['coin']} {p['size']:+.4f} @ {p['entry_px']:.2f}")
    pos_str = "，".join(pos_parts) if pos_parts else "无持仓"

    try:
        state = info.user_state(wallet)
        equity = float(state.get("marginSummary", {}).get("accountValue", 0))
    except:
        equity = 0

    # Service
    try:
        svc = subprocess.run(["systemctl", "is-active", "ws-monitor"],
                             capture_output=True, text=True, timeout=5)
        svc_status = svc.stdout.strip()
    except:
        svc_status = "unknown"

    # Verify coins
    for coin in ["BTC", "ETH"]:
        verify_coin(info, coin, cfg, report)

    has_issues = any("❌" in l for l in report)

    # Header
    if has_issues or svc_status != "active":
        header = "⚠️ **HL 验证有问题**"
    else:
        header = "✅ **HL 验证通过**"

    final = [header]
    final.append(f"${equity:.0f}｜{pos_str}｜服务: {svc_status}")
    final.extend(report)
    if svc_status != "active":
        final.append(f"❌ 服务异常: {svc_status}")
    report = final

    output = "\n".join(report)
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
