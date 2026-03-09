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
    has_issues = False

    # 1. Fetch candles
    try:
        candles = get_recent_candles_from_exchange(cfg.instId, "30m", 300)
        closes = [c["close"] for c in candles]
    except Exception as e:
        report.append(f"❌ OKX BB: 无法获取K线 {e}")
        print("\n".join(report))
        return 1

    # 2. Calculate BB + trend using production code
    idx = len(closes) - 1
    bb = get_bb_levels(closes, cfg.strategy.bb_period, cfg.strategy.bb_multiplier, idx)
    if bb is None:
        report.append("❌ OKX BB: BB计算失败（数据不足）")
        print("\n".join(report))
        return 1

    mid, upper, lower = bb

    period = cfg.strategy.trend_ema_period
    lookback = cfg.strategy.trend_lookback
    ema_start = max(0, idx - period * 3)
    ema_vals = ema(closes[ema_start:idx + 1], period)
    if len(ema_vals) >= lookback + 1:
        trend = "up" if ema_vals[-1] > ema_vals[-1 - lookback] else "down"
    else:
        trend = "unknown"

    # 3. Signal check
    prev_close = closes[-1]
    buffer = cfg.execution.entry_buffer_pct
    long_cond = trend == "up" and prev_close > upper * (1 + buffer)
    short_cond = trend == "down" and prev_close < lower * (1 - buffer)

    if long_cond:
        signal_str = f"🟢 LONG 触发（close {prev_close:.2f} 突破上轨 {upper:.2f}）"
    elif short_cond:
        signal_str = f"🔴 SHORT 触发（close {prev_close:.2f} 跌破下轨 {lower:.2f}）"
    else:
        if trend == "up":
            gap = upper * (1 + buffer) - prev_close
            signal_str = f"HOLD｜趋势向上，需涨 {gap:.1f}（{gap/prev_close*100:.1f}%）突破上轨才开多"
        elif trend == "down":
            gap = prev_close - lower * (1 - buffer)
            signal_str = f"HOLD｜趋势向下，需跌 {gap:.1f}（{gap/prev_close*100:.1f}%）跌破下轨才开空"
        else:
            signal_str = "HOLD｜趋势不明"

    # 4. Compare with ws_monitor log
    log_ok = True
    log_detail = ""
    cc_lines = get_ws_monitor_log_last_cc(5)
    if cc_lines and "(could not" not in cc_lines[0]:
        last_cc = parse_cc_line(cc_lines[-1])
        if last_cc:
            tol = mid * 0.015
            discrepancies = []
            if abs(last_cc["upper"] - upper) > tol:
                discrepancies.append(f"upper: 日志{last_cc['upper']:.2f} vs 计算{upper:.2f}")
            if abs(last_cc["lower"] - lower) > tol:
                discrepancies.append(f"lower: 日志{last_cc['lower']:.2f} vs 计算{lower:.2f}")
            if last_cc["trend"] != trend and trend != "unknown":
                discrepancies.append(f"trend: 日志{last_cc['trend']} vs 计算{trend}")
            if discrepancies:
                log_ok = False
                log_detail = "；".join(discrepancies)
    else:
        log_ok = False
        log_detail = "无法读取日志"

    # 5. Position + service
    pos = get_position_state()
    pos_str = "无持仓"
    if pos:
        pos_str = f"{pos.get('direction','?')} @ {pos.get('entry_price','?')}, SL={pos.get('sl_price','N/A')}"

    try:
        svc = subprocess.run(["systemctl", "is-active", "okx-bb-monitor"],
                             capture_output=True, text=True, timeout=5)
        svc_status = svc.stdout.strip()
    except:
        svc_status = "unknown"

    # Build compact report
    if log_ok and svc_status == "active":
        report.append(f"✅ **OKX BB 验证通过**")
    else:
        has_issues = True
        report.append(f"⚠️ **OKX BB 验证有问题**")

    report.append(f"ETH {prev_close:.2f}｜BB {lower:.1f}-{upper:.1f}｜trend {trend}｜{signal_str}")
    report.append(f"仓位: {pos_str}｜服务: {svc_status}")

    if not log_ok:
        report.append(f"⚠️ 日志差异: {log_detail}")
    if svc_status != "active":
        report.append(f"❌ 服务异常: {svc_status}")

    output = "\n".join(report)
    print(output)
    return 1 if has_issues else 0


if __name__ == "__main__":
    sys.exit(main())
