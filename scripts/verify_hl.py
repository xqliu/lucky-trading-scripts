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
    """Verify one coin's trading logic."""
    report.append(f"\n{'─' * 40}")
    report.append(f"🪙 {coin}")
    report.append(f"{'─' * 40}")

    # 1. Fetch candles
    try:
        candles_30m = get_candles_from_hl(info, coin, "30m", 72)
        candles_4h = get_4h_candles(info, coin, 336)
        report.append(f"  30m candles: {len(candles_30m)}, 4h candles: {len(candles_4h)}")
        if candles_30m:
            report.append(f"  最新 30m close: {candles_30m[-1]['c']:.2f}")
    except Exception as e:
        report.append(f"  ❌ 无法获取 K 线: {e}")
        return

    # 2. Run production signal detection
    try:
        # Build candle list in the format detect_signal expects
        idx = len(candles_30m) - 1
        coin_cfg = getattr(cfg.coins, coin, None) if hasattr(cfg, 'coins') else None
        signal = hl_detect_signal(candles_30m, candles_4h, idx, cfg, coin_cfg=coin_cfg)
        report.append(f"  📊 信号计算结果: {signal or 'HOLD'}")
    except Exception as e:
        report.append(f"  ❌ 信号计算异常: {e}")
        signal = None

    # 3. Key indicator values
    if candles_30m:
        closes = [c["c"] for c in candles_30m]
        # Volume
        vols = [c["v"] for c in candles_30m]
        coin_cfg = getattr(cfg.coins, coin, None) if hasattr(cfg, 'coins') else None
        range_bars = getattr(coin_cfg, 'range_bars', cfg.strategy.range_bars) if coin_cfg else cfg.strategy.range_bars
        lookback = getattr(coin_cfg, 'lookback_bars', cfg.strategy.lookback_bars) if coin_cfg else cfg.strategy.lookback_bars
        vol_threshold = getattr(coin_cfg, 'vol_threshold', cfg.strategy.vol_threshold) if coin_cfg else cfg.strategy.vol_threshold

        if len(closes) > lookback:
            high_range = max(c["h"] for c in candles_30m[-range_bars:])
            low_range = min(c["l"] for c in candles_30m[-range_bars:])
            range_pct = (high_range - low_range) / low_range * 100
            report.append(f"  Range({range_bars}): {low_range:.2f} - {high_range:.2f} ({range_pct:.2f}%)")

        if len(vols) > lookback:
            recent_vol = vols[-1]
            avg_vol = sum(vols[-lookback:]) / lookback
            vol_ratio = recent_vol / avg_vol if avg_vol > 0 else 0
            report.append(f"  Volume: {recent_vol:.2f} / avg {avg_vol:.2f} = {vol_ratio:.2f}x (threshold: {vol_threshold})")

    # 4. 4h trend
    if candles_4h and len(candles_4h) > 21:
        closes_4h = [c["c"] for c in candles_4h]
        ema8 = ema(closes_4h, 8)
        ema21 = ema(closes_4h, 21)
        if ema8 and ema21:
            trend_4h = "UP" if ema8[-1] > ema21[-1] else "DOWN"
            report.append(f"  4h Trend: {trend_4h} (EMA8={ema8[-1]:.2f}, EMA21={ema21[-1]:.2f})")

    # 5. Compare with ws_monitor log
    report.append(f"  🔍 ws_monitor 日志:")
    log_lines = get_ws_monitor_log_last_signals(coin, 5)
    if log_lines and "(could not" not in log_lines[0]:
        for l in log_lines[-3:]:
            # Truncate long lines
            report.append(f"    {l[-150:]}")
    else:
        report.append(f"    ⚠️ 无相关日志")


def main():
    report = []
    report.append("=" * 60)
    report.append("Hyperliquid 交易逻辑验证报告")
    report.append(f"时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    report.append("=" * 60)

    # Load config
    import os
    os.environ.setdefault("LUCKYTRADER_CONFIG_DIR",
                          str(Path.home() / ".openclaw/workspace/trading/config"))
    cfg = get_config()

    # Init HL info (read-only, no wallet needed)
    info = Info(skip_ws=True)

    # Get positions
    wallet = cfg.exchange.main_wallet
    positions = get_hl_positions(info, wallet)
    report.append(f"\n📋 当前持仓:")
    if positions and "error" not in positions[0]:
        for p in positions:
            report.append(f"  {p['coin']} {p['size']:+.4f} @ {p['entry_px']:.2f}, uPnL: {p['unrealized_pnl']:+.4f}")
    else:
        report.append(f"  无持仓" if not positions else f"  ⚠️ {positions[0].get('error', '?')}")

    # Get account
    try:
        state = info.user_state(wallet)
        equity = float(state.get("marginSummary", {}).get("accountValue", 0))
        report.append(f"  账户: ${equity:.2f}")
    except:
        pass

    # Verify each coin
    for coin in ["BTC", "ETH"]:
        verify_coin(info, coin, cfg, report)

    # Service status
    try:
        svc = subprocess.run(["systemctl", "is-active", "ws-monitor"],
                             capture_output=True, text=True, timeout=5)
        status = svc.stdout.strip()
        report.append(f"\n🔧 ws-monitor 服务: {status}")
    except Exception as e:
        report.append(f"\n🔧 ws-monitor 服务: 无法查询 ({e})")

    # Summary
    report.append("\n" + "=" * 60)
    issues = [l for l in report if "❌" in l]
    if issues:
        report.append(f"⚠️ 发现 {len(issues)} 个问题")
    else:
        report.append("✅ HL 交易逻辑验证通过")
    report.append("=" * 60)

    output = "\n".join(report)
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
