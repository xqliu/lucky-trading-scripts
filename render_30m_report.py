#!/usr/bin/env python3
"""Compact 30-min market report — same data, tighter layout."""
from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / 'repos' / 'lucky-trading-scripts'))

from hyperliquid.info import Info
from luckytrader.signal import analyze


def load_hl_wallet():
    from luckytrader.config import get_config
    return get_config().exchange.main_wallet


def render_okx_raw() -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        from okx_bb.render_status_block import main as okx_main
        okx_main()
    return buf.getvalue().strip()


def fmt_m(v: float) -> str:
    if abs(v) >= 1e9:
        return f"${v/1e9:.1f}B"
    return f"${v/1e6:.1f}M"


def render_hl() -> str:
    info = Info(skip_ws=True)
    state = info.user_state(load_hl_wallet())
    acct = float(state['marginSummary']['accountValue'])

    positions = []
    for ap in state.get('assetPositions', []):
        pos = ap.get('position', {})
        szi = float(pos.get('szi', 0) or 0)
        if szi == 0:
            continue
        coin = pos.get('coin', '?')
        d = 'L' if szi > 0 else 'S'
        entry = float(pos.get('entryPx') or 0)
        upl = float(pos.get('unrealizedPnl') or 0)
        roe = float(pos.get('returnOnEquity') or 0) * 100
        lev = pos.get('leverage', {}).get('value', '?')
        positions.append(f"{coin} {d} {abs(szi):g} @ {entry:.0f} | uPnL {upl:+.2f} ({roe:+.1f}%) | {lev}x")

    if positions:
        return f"**HL** ${acct:.2f} | " + " | ".join(positions)
    return f"**HL** ${acct:.2f} | 空仓"


def render_okx() -> str:
    raw = render_okx_raw()
    # Parse key fields from the raw block
    import re
    acct_m = re.search(r'\*\*账户\*\*: \$([0-9.]+)', raw)
    acct = acct_m.group(1) if acct_m else '?'

    # Check position
    if '当前持仓**: 无' in raw or '**当前持仓**: 无' in raw:
        pos_str = '空仓'
    else:
        # Try to extract position line
        pos_m = re.search(r'- (\w+ \w+ [0-9.]+张 @ \$[0-9.]+ \| 未实现: \$[^\n]+)', raw)
        pos_str = pos_m.group(1) if pos_m else '有持仓'

    # SL/TP
    sl_m = re.search(r'SL: \$([0-9.]+)', raw)
    tp_m = re.search(r'TP: \$([0-9.]+)', raw)
    sl_tp = ''
    if sl_m and tp_m:
        sl_tp = f" | SL {sl_m.group(1)} TP {tp_m.group(1)}"

    # Status check
    if '✅' in raw:
        status = ' | ✅一致'
    elif '🚨' in raw:
        status = ' | 🚨不一致'
    else:
        status = ''

    return f"**OKX** ${acct} | {pos_str}{sl_tp}{status}"


def render_coin(r: dict) -> str:
    b = r.get('breakout', {})
    vol_ok = '✅' if b.get('vol_confirm') else '❌'
    up_ok = '✅' if b.get('up') else '❌'
    dn_ok = '✅' if b.get('down') else '❌'

    lines = [
        f"**{r['coin']}** ${r['price']:,.0f} | {r['trend']} (8/21: {r['ema_8']:,.0f}/{r['ema_21']:,.0f}) | 4h {r.get('trend_4h','N/A')} | RSI {r['rsi']:.1f}",
        f"量: {fmt_m(r['volume_usd'])} (均{fmt_m(r['avg_volume_24h'])}, {r['volume_ratio']:.2f}x) | 区间: {r['low_24h']:,.0f}-{r['high_24h']:,.0f} ({r['range_24h']:.1f}%)",
        f"突破: ↑{r['high_24h']:,.0f} {up_ok} ↓{r['low_24h']:,.0f} {dn_ok} | 放量 {vol_ok}",
    ]

    if r['supports']:
        lines.append("支撑: " + " ".join(f"{s[0]:,.0f}({s[1]})" for s in r['supports']))
    if r['resistances']:
        lines.append("阻力: " + " ".join(f"{s[0]:,.0f}({s[1]})" for s in r['resistances']))

    sig = r['signal']
    if r['signal_reasons']:
        sig += f" — {'; '.join(r['signal_reasons'])}"
    if r.get('signal_filtered'):
        sig += f" | 🚫{r['signal_filtered']}"
    lines.append(f"⚡ {sig}")

    if 'suggested_stop' in r:
        from luckytrader.config import get_config
        _c = get_config()
        lines.append(f"🛑SL ${r['suggested_stop']:,.0f} (-{_c.risk.stop_loss_pct*100:.0f}%) | 🎯TP ${r['suggested_tp']:,.0f} (+{_c.risk.take_profit_pct*100:.0f}%) | ⏰{_c.risk.max_hold_hours}h")

    return '\n'.join(lines)


def render_shared(r: dict) -> str:
    lines = []
    ctx = r.get('market_context', {})
    if ctx:
        lines.append('**费率&OI**')
        for cn in ('BTC', 'ETH'):
            c = ctx.get(cn)
            if c:
                fr = c['funding_rate']
                fr_a = fr * 24 * 365 * 100
                oi = c['open_interest'] * c['mark_price']
                lines.append(f"{cn}: {fr*100:.4f}%/h ({fr_a:+.1f}%年化) | OI {fmt_m(oi)}")

    trades = r.get('recent_trades', [])
    if trades:
        from datetime import datetime, timezone, timedelta
        _CST = timezone(timedelta(hours=8))
        lines.append('\n**最近交易**')
        for t in trades:
            def _ft(ts):
                return datetime.fromtimestamp(ts/1000, tz=timezone.utc).astimezone(_CST).strftime('%m-%d %H:%M')
            d = 'L' if t['direction'] == 'LONG' else 'S'
            if t['status'] == 'closed' and t['open_price']:
                pnl = f" | {t['pnl']:+.2f}U" if t['pnl'] is not None else ''
                lines.append(f"{t['coin']} {d} {_ft(t['open_time'])} {t['open_price']:,.0f}→{_ft(t['close_time'])} {t['close_price']:,.0f}{pnl}")
            elif t['status'] == 'open':
                lines.append(f"{t['coin']} {d} {_ft(t['open_time'])} {t['open_price']:,.0f}→持仓中")

    return '\n'.join(lines)


def render_conclusion(btc: dict, eth: dict) -> str:
    if btc['signal'] == 'HOLD' and eth['signal'] == 'HOLD':
        return '**结论** BTC/ETH 均未突破，继续等待量能放大或区间突破。'
    return f"**结论** 信号：BTC={btc['signal']} / ETH={eth['signal']}，按风控执行。"


def build_part1(btc: dict) -> str:
    return '\n'.join([
        '📊 **30分钟市场报告**',
        '',
        render_hl(),
        render_okx(),
        '',
        render_coin(btc),
    ])


def build_part2(eth: dict, btc: dict) -> str:
    return '\n'.join([
        render_coin(eth),
        '',
        render_shared(eth),
        '',
        render_conclusion(btc, eth),
    ])


def main() -> int:
    part = sys.argv[1] if len(sys.argv) > 1 else 'all'
    btc = analyze('BTC')
    eth = analyze('ETH')

    if part == '1':
        print(build_part1(btc))
    elif part == '2':
        print(build_part2(eth, btc))
    else:
        print(build_part1(btc))
        print('\n---PART2---\n')
        print(build_part2(eth, btc))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
