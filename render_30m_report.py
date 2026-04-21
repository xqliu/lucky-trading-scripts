#!/usr/bin/env python3
"""Compact 30-min market report — same data, tighter layout."""
from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / 'repos' / 'lucky-trading-scripts'))

from luckytrader.signal import analyze, get_user_state


def load_hl_wallet():
    from luckytrader.config import get_config
    return get_config().exchange.main_wallet


def render_okx_raw() -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        from okx_bb.render_status_block import main as okx_main
        okx_main()
    return buf.getvalue().strip()


def render_okx_sol_raw() -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        from okx_sol_bb.render_status_block import main as sol_main
        sol_main()
    return buf.getvalue().strip()


def fmt_m(v: float) -> str:
    if abs(v) >= 1e9:
        return f"${v/1e9:.1f}B"
    return f"${v/1e6:.1f}M"


def render_hl() -> str:
    state = get_user_state(load_hl_wallet())
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


def render_okx_sol() -> str:
    raw = render_okx_sol_raw()
    import re
    acct_m = re.search(r'\*\*账户\*\*: \$([0-9.]+)', raw)
    acct = acct_m.group(1) if acct_m else '?'

    sol_m = re.search(r'\*\*SOL\*\*: \$([0-9.]+)', raw)
    sol_price = sol_m.group(1) if sol_m else '?'

    bb_m = re.search(r'\*\*BB\([^)]+\)\*\*: ([0-9.]+) - ([0-9.]+) - ([0-9.]+)', raw)
    bb_str = f" | BB {bb_m.group(1)}-{bb_m.group(3)}" if bb_m else ""

    if '当前持仓**: 无' in raw or '**当前持仓**: 无' in raw:
        pos_str = '空仓'
    else:
        pos_m = re.search(r'- (\w+ \w+ [0-9.]+张 @ \$[0-9.]+ \| 未实现: \$[^\n]+)', raw)
        pos_str = pos_m.group(1) if pos_m else '有持仓'

    sl_m = re.search(r'SL: \$([0-9.]+)', raw)
    tp_m = re.search(r'TP: \$([0-9.]+)', raw)
    sl_tp = ''
    if sl_m and tp_m:
        sl_tp = f" | SL {sl_m.group(1)} TP {tp_m.group(1)}"

    status = ''
    if '✅' in raw:
        status = ' | ✅'
    elif '🚨' in raw:
        status = ' | 🚨不一致'

    return f"**OKX SOL** ${acct} | SOL ${sol_price}{bb_str} | {pos_str}{sl_tp}{status}"


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

    return '\n'.join(lines)


def render_conclusion(btc: dict, eth: dict) -> str:
    lines = ['**结论**']

    # Determine market regime
    btc_vol = btc.get('volume_ratio', 0)
    eth_vol = eth.get('volume_ratio', 0)
    btc_b = btc.get('breakout', {})
    eth_b = eth.get('breakout', {})

    if btc['signal'] != 'HOLD' or eth['signal'] != 'HOLD':
        sigs = []
        if btc['signal'] != 'HOLD':
            sigs.append(f"BTC {btc['signal']}")
        if eth['signal'] != 'HOLD':
            sigs.append(f"ETH {eth['signal']}")
        lines.append(f"出现信号：{' / '.join(sigs)}，按风控参数执行。")
        if btc['signal'] != 'HOLD' and 'suggested_stop' in btc:
            lines.append(f"BTC 入场后关注 SL {btc['suggested_stop']:,.0f} / TP {btc['suggested_tp']:,.0f}。")
        if eth['signal'] != 'HOLD' and 'suggested_stop' in eth:
            lines.append(f"ETH 入场后关注 SL {eth['suggested_stop']:,.0f} / TP {eth['suggested_tp']:,.0f}。")
    else:
        # No signal — give context on why
        has_vol = btc_vol > 1.25 or eth_vol > 1.25
        near_breakout_up = btc_b.get('up') or eth_b.get('up')
        near_breakout_dn = btc_b.get('down') or eth_b.get('down')

        if has_vol and not (near_breakout_up or near_breakout_dn):
            lines.append('量能放大但未触及区间边界，关注是否向上/下试探。')
        elif (near_breakout_up or near_breakout_dn) and not has_vol:
            lines.append('价格接近区间边界但量能不足，需放量确认才构成有效突破。')
        else:
            lines.append('BTC/ETH 均在区间内横盘，量能不足，无突破条件。')

        # RSI context
        btc_rsi = btc.get('rsi', 50)
        eth_rsi = eth.get('rsi', 50)
        if btc_rsi > 65 or eth_rsi > 65:
            lines.append(f"RSI 偏高（BTC {btc_rsi:.0f} / ETH {eth_rsi:.0f}），短期追多风险增大。")
        elif btc_rsi < 35 or eth_rsi < 35:
            lines.append(f"RSI 偏低（BTC {btc_rsi:.0f} / ETH {eth_rsi:.0f}），关注超卖反弹机会。")
        else:
            lines.append(f"RSI 中性（BTC {btc_rsi:.0f} / ETH {eth_rsi:.0f}），继续等待方向选择。")

        lines.append('策略：不追单，等放量突破信号。')

    return '\n'.join(lines)


def build_part1(btc: dict) -> str:
    return '\n'.join([
        '📊 **30分钟市场报告**',
        '',
        render_hl(),
        render_okx(),
        render_okx_sol(),
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
