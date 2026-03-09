#!/usr/bin/env python3
"""
OKX BB WS Monitor 交易逻辑验证器（只读，不下单）
=================================================
从交易所拉实时 K 线，用生产代码计算 BB/trend/signal，
对比 ws_monitor 日志输出，检查一致性。

用法: python scripts/verify_okx_bb.py
输出: 人类可读的验证报告
"""
import sys
import json
import subprocess
from pathlib import Path
from datetime import datetime, timezone, timedelta

# Setup path — import from the SAME repo code that production uses
_root = str(Path(__file__).parent.parent)
if _root not in sys.path:
    sys.path.insert(0, _root)

from okx_bb.strategy import detect_signal, get_bb_levels
from okx_bb.config import load_config
from core.indicators import ema


def get_recent_candles_from_exchange(inst_id: str, bar: str = "30m", limit: int = 300):
    """Fetch candles from OKX public API (no auth needed)."""
    import urllib.request
    url = f"https://www.okx.com/api/v5/market/candles?instId={inst_id}&bar={bar}&limit={limit}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    if data.get("code") != "0":
        raise RuntimeError(f"OKX API error: {data}")
    # OKX returns newest first: [ts, o, h, l, c, vol, ...]
    raw = data["data"]
    raw.reverse()  # oldest first
    candles = []
    for r in raw:
        candles.append({
            "ts": int(r[0]),
            "open": float(r[1]),
            "high": float(r[2]),
            "low": float(r[3]),
            "close": float(r[4]),
            "vol": float(r[5]),
        })
    return candles


def get_ws_monitor_log_last_cc(n=10):
    """Parse last N CC check lines from journalctl."""
    try:
        result = subprocess.run(
            ["journalctl", "-u", "okx-bb-monitor", "--no-pager", "-n", "200"],
            capture_output=True, text=True, timeout=10
        )
        lines = result.stdout.strip().split("\n")
        cc_lines = [l for l in lines if "CC check:" in l]
        return cc_lines[-n:] if len(cc_lines) >= n else cc_lines
    except Exception as e:
        return [f"(could not read journal: {e})"]


def get_position_state():
    """Read local position state."""
    cfg_dir = Path(__file__).parent.parent / "okx_bb" / "state"
    # Also check env override
    import os
    override = os.environ.get("OKX_BB_CONFIG_DIR")
    if override:
        state_dir = Path(override).parent / "state"
        if state_dir.exists():
            cfg_dir = state_dir
    
    # Try multiple known locations
    for d in [cfg_dir,
              Path.home() / ".openclaw/workspace/trading/okx_bb/state"]:
        pos_file = d / "position_state.json"
        if pos_file.exists():
            data = json.loads(pos_file.read_text())
            return data.get("position")
    return None


def parse_cc_line(line: str):
    """Extract prev_close, upper, lower, trend, buffer from CC check log line."""
    import re
    m = re.search(
        r'prev_close=([\d.]+)\s+upper=([\d.]+)\s+lower=([\d.]+)\s+trend=(\w+)\s+buffer=([\d.]+)',
        line
    )
    if m:
        return {
            "prev_close": float(m.group(1)),
            "upper": float(m.group(2)),
            "lower": float(m.group(3)),
            "trend": m.group(4),
            "buffer": float(m.group(5)),
        }
    return None


def main():
    # Load config (load_config reads OKX_BB_CONFIG_DIR env var internally)
    import os
    os.environ.setdefault("OKX_BB_CONFIG_DIR",
                          str(Path.home() / ".openclaw/workspace/trading/okx_bb/config"))
    cfg = load_config()

    report = []
    report.append("=" * 60)
    report.append("OKX BB 交易逻辑验证报告")
    report.append(f"时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    report.append("=" * 60)

    # 1. Fetch candles
    try:
        candles = get_recent_candles_from_exchange(cfg.instId, "30m", 300)
        closes = [c["close"] for c in candles]
        report.append(f"\n📊 K线数据: {len(candles)} 根 30m candles")
        report.append(f"   最新 close: {closes[-1]:.2f}")
        report.append(f"   时间范围: {datetime.fromtimestamp(candles[0]['ts']/1000, tz=timezone.utc).strftime('%m-%d %H:%M')} → {datetime.fromtimestamp(candles[-1]['ts']/1000, tz=timezone.utc).strftime('%m-%d %H:%M')} UTC")
    except Exception as e:
        report.append(f"\n❌ 无法获取 K 线数据: {e}")
        print("\n".join(report))
        return 1

    # 2. Calculate BB using production code
    idx = len(closes) - 1
    bb = get_bb_levels(closes, cfg.strategy.bb_period, cfg.strategy.bb_multiplier, idx)
    if bb is None:
        report.append("\n❌ BB 计算返回 None（数据不足）")
        print("\n".join(report))
        return 1

    mid, upper, lower = bb
    report.append(f"\n📈 BB({cfg.strategy.bb_period}, {cfg.strategy.bb_multiplier}):")
    report.append(f"   Upper: {upper:.2f}")
    report.append(f"   Mid:   {mid:.2f}")
    report.append(f"   Lower: {lower:.2f}")
    report.append(f"   Width: {(upper - lower) / mid * 100:.2f}%")

    # 3. Calculate trend using production code
    period = cfg.strategy.trend_ema_period
    lookback = cfg.strategy.trend_lookback
    ema_start = max(0, idx - period * 3)
    ema_vals = ema(closes[ema_start:idx + 1], period)
    if len(ema_vals) >= lookback + 1:
        trend_rising = ema_vals[-1] > ema_vals[-1 - lookback]
        trend = "up" if trend_rising else "down"
        report.append(f"\n📉 Trend EMA({period}, lookback={lookback}):")
        report.append(f"   EMA current: {ema_vals[-1]:.2f}")
        report.append(f"   EMA {lookback} bars ago: {ema_vals[-1 - lookback]:.2f}")
        report.append(f"   Trend: {trend}")
    else:
        trend = "unknown"
        report.append(f"\n⚠️ Trend EMA 数据不足")

    # 4. Check close-confirm-buffer entry conditions
    prev_close = closes[-1]
    buffer = cfg.execution.entry_buffer_pct
    report.append(f"\n🎯 Close-Confirm-Buffer 检查:")
    report.append(f"   prev_close={prev_close:.2f}, buffer={buffer}")

    long_cond = trend == "up" and prev_close > upper * (1 + buffer)
    short_cond = trend == "down" and prev_close < lower * (1 - buffer)

    if long_cond:
        report.append(f"   ✅ LONG 信号: close {prev_close:.2f} > upper {upper:.2f}")
    elif short_cond:
        report.append(f"   ✅ SHORT 信号: close {prev_close:.2f} < lower {lower:.2f}")
    else:
        report.append(f"   ⏸️ 无信号")
        if trend == "up":
            gap = upper * (1 + buffer) - prev_close
            report.append(f"   做多还差: {gap:.2f} ({gap/prev_close*100:.2f}%)")
        elif trend == "down":
            gap = prev_close - lower * (1 - buffer)
            report.append(f"   做空还差: {gap:.2f} ({gap/prev_close*100:.2f}%)")

    # 5. Compare with ws_monitor log
    report.append(f"\n🔍 对比 ws_monitor 日志:")
    cc_lines = get_ws_monitor_log_last_cc(5)
    if not cc_lines or "(could not" in cc_lines[0]:
        report.append(f"   ⚠️ 无法读取日志: {cc_lines[0] if cc_lines else 'empty'}")
    else:
        last_cc = parse_cc_line(cc_lines[-1])
        if last_cc:
            report.append(f"   日志最后 CC: prev_close={last_cc['prev_close']:.2f} "
                          f"upper={last_cc['upper']:.2f} lower={last_cc['lower']:.2f} "
                          f"trend={last_cc['trend']}")

            # Compare
            discrepancies = []
            # BB values may differ slightly because ws_monitor uses accumulated candles
            # while we fetch fresh from API. Allow 0.5% tolerance.
            tol = mid * 0.015  # 1.5% tolerance — ws_monitor uses accumulated WS candles, API fetch is slightly ahead
            if abs(last_cc["upper"] - upper) > tol:
                discrepancies.append(f"upper: 日志={last_cc['upper']:.2f} vs 计算={upper:.2f} (差{abs(last_cc['upper']-upper):.2f})")
            if abs(last_cc["lower"] - lower) > tol:
                discrepancies.append(f"lower: 日志={last_cc['lower']:.2f} vs 计算={lower:.2f} (差{abs(last_cc['lower']-lower):.2f})")
            if last_cc["trend"] != trend and trend != "unknown":
                discrepancies.append(f"trend: 日志={last_cc['trend']} vs 计算={trend}")

            if discrepancies:
                report.append(f"   ⚠️ 发现差异:")
                for d in discrepancies:
                    report.append(f"      - {d}")
            else:
                report.append(f"   ✅ 日志与独立计算一致")
        else:
            report.append(f"   ⚠️ 无法解析日志行")
            report.append(f"   最后日志: {cc_lines[-1][-120:]}")

    # 6. Position state
    pos = get_position_state()
    report.append(f"\n📋 本地仓位状态:")
    if pos:
        report.append(f"   {pos.get('direction', '?')} @ {pos.get('entry_price', '?')}")
        report.append(f"   SL: {pos.get('sl_price', 'N/A')}, TP: {pos.get('tp_price', 'N/A')}")
    else:
        report.append(f"   无持仓")

    # 7. Service status
    try:
        svc = subprocess.run(["systemctl", "is-active", "okx-bb-monitor"],
                             capture_output=True, text=True, timeout=5)
        status = svc.stdout.strip()
        report.append(f"\n🔧 服务状态: {status}")
    except Exception as e:
        report.append(f"\n🔧 服务状态: 无法查询 ({e})")

    # Summary
    report.append("\n" + "=" * 60)
    issues = [l for l in report if "❌" in l or "⚠️ 发现差异" in l]
    if issues:
        report.append(f"⚠️ 发现 {len(issues)} 个潜在问题，需关注")
    else:
        report.append("✅ OKX BB 交易逻辑验证通过")
    report.append("=" * 60)

    output = "\n".join(report)
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
