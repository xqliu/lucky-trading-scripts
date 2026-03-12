#!/usr/bin/env python3
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


def render_okx_block() -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        from okx_bb.render_status_block import main as okx_main
        okx_main()
    return buf.getvalue().strip()


def render_hl_block() -> str:
    info = Info(skip_ws=True)
    state = info.user_state(load_hl_wallet())
    acct_val = float(state['marginSummary']['accountValue'])
    withdraw = float(state['withdrawable'])

    lines = [
        '**30分钟市场报告**',
        '',
        '**Section 1 HL**',
        f'- 账户权益：${acct_val:.2f}',
        f'- 可提现：${withdraw:.2f}',
    ]

    has_pos = False
    for ap in state.get('assetPositions', []):
        pos = ap.get('position', {})
        szi = float(pos.get('szi', 0) or 0)
        if szi == 0:
            continue
        has_pos = True
        coin = pos.get('coin', '?')
        direction = '多头' if szi > 0 else '空头'
        entry = float(pos.get('entryPx') or 0)
        pos_val = float(pos.get('positionValue') or 0)
        upl = float(pos.get('unrealizedPnl') or 0)
        roe = float(pos.get('returnOnEquity') or 0) * 100
        lev = pos.get('leverage', {}).get('value', '?')
        liq = pos.get('liquidationPx')
        liq_text = f'{float(liq):.2f}' if liq not in (None, '') else 'N/A'
        lines.append(f'- 当前持仓：{coin} {direction} {abs(szi):g}')
        lines.append(f'- 开仓价：{entry:.2f}')
        lines.append(f'- 仓位价值：${pos_val:.2f}')
        lines.append(f'- 未实现盈亏：${upl:.2f}')
        lines.append(f'- ROE：{roe:.2f}% | 杠杆：{lev}x | 强平价：{liq_text}')

    if not has_pos:
        lines.append('- 当前持仓：无')

    return '\n'.join(lines)


def format_coin_section(result: dict) -> str:
    lines = []
    lines.append(f"🪙 {result['coin']}")
    lines.append(f"💰 价格: ${result['price']:,.0f}")
    lines.append(f"📊 成交量: ${result['volume_usd']:,.0f} (均值: ${result['avg_volume_24h']:,.0f}, {result['volume_ratio']:.2f}x)")
    lines.append(f"📏 区间: ${result['low_24h']:,.0f} - ${result['high_24h']:,.0f} ({result['range_24h']:.1f}%)")
    lines.append(f"📈 趋势: {result['trend']} (EMA8: {result['ema_8']:,.0f} / EMA21: {result['ema_21']:,.0f}) | 4h趋势: {result.get('trend_4h', 'N/A')}")
    lines.append(f"📉 RSI: {result['rsi']:.1f}")

    b = result['breakout']
    vol_str = f"放量{b['vol_ratio_30m']:.1f}x" if b['vol_confirm'] else f"量{b['vol_ratio_30m']:.1f}x"
    lines.append(f"\n🟢 做多: 突破${result['high_24h']:,.0f} {'✅' if b['up'] else '❌'} + {vol_str} {'✅' if b['vol_confirm'] else '❌'}")
    lines.append(f"🔴 做空: 跌破${result['low_24h']:,.0f} {'✅' if b['down'] else '❌'} + {vol_str} {'✅' if b['vol_confirm'] else '❌'}")

    if result['supports']:
        lines.append(f"\n🛡️ 支撑: {', '.join(f'${s[0]:,.0f}({s[1]}次)' for s in result['supports'])}")
    if result['resistances']:
        lines.append(f"🚧 阻力: {', '.join(f'${r[0]:,.0f}({r[1]}次)' for r in result['resistances'])}")

    sig = result['signal']
    if result['signal_reasons']:
        sig += f" — {'; '.join(result['signal_reasons'])}"
    lines.append(f"\n⚡ 信号: {sig}")
    if result.get('signal_filtered'):
        lines.append(f"🚫 过滤: {result['signal_filtered']}")

    if 'suggested_stop' in result:
        from luckytrader.config import get_config
        _c = get_config()
        lines.append(f"🛑 止损: ${result['suggested_stop']:,.0f} (-{_c.risk.stop_loss_pct*100:.0f}%)")
        lines.append(f"🎯 止盈: ${result['suggested_tp']:,.0f} (+{_c.risk.take_profit_pct*100:.0f}%)")
        lines.append(f"⏰ 持仓上限: {_c.risk.max_hold_hours}h")

    return '\n'.join(lines)


def render_shared_context(result: dict) -> str:
    lines = []
    ctx = result.get('market_context', {})
    if ctx:
        lines.append('💹 资金费率 & OI:')
        for coin_name in ('BTC', 'ETH'):
            c = ctx.get(coin_name)
            if c:
                fr = c['funding_rate']
                fr_annual = fr * 24 * 365 * 100
                oi_usd = c['open_interest'] * c['mark_price']
                lines.append(f"  {coin_name}: 费率 {fr*100:.4f}%/h ({fr_annual:+.1f}%年化) | OI ${oi_usd/1e9:.2f}B | ${c['mark_price']:,.0f}")

    trades = result.get('recent_trades', [])
    if trades:
        from datetime import datetime, timezone, timedelta
        _CST = timezone(timedelta(hours=8))
        lines.append('\n📋 最近交易:')
        for t in trades:
            def _fmt_time(ts):
                return datetime.fromtimestamp(ts/1000, tz=timezone.utc).astimezone(_CST).strftime('%m-%d %H:%M')
            if t['status'] == 'closed' and t['open_price']:
                open_t = _fmt_time(t['open_time'])
                close_t = _fmt_time(t['close_time'])
                pnl_str = f" | {'+' if t['pnl'] >= 0 else ''}{t['pnl']:.2f}U" if t['pnl'] is not None else ''
                lines.append(f"  {t['coin']} {t['direction']} {open_t} {t['open_price']:,.0f}→{close_t} {t['close_price']:,.0f}{pnl_str}")
            elif t['status'] == 'open':
                open_t = _fmt_time(t['open_time'])
                lines.append(f"  {t['coin']} {t['direction']} {open_t} {t['open_price']:,.0f}→持仓中")

    return '\n'.join(lines)


def render_conclusion(btc: dict, eth: dict) -> str:
    lines = ['**结论**']
    if btc['signal'] == 'HOLD' and eth['signal'] == 'HOLD':
        lines.append('- BTC / ETH 均未形成可执行突破，继续等待。')
        lines.append('- 当前优先级是观察量能是否继续放大，以及是否有效突破区间边界。')
    else:
        lines.append(f"- 出现可执行信号：BTC={btc['signal']} / ETH={eth['signal']}")
        lines.append('- 按既定风控执行，不主观追单。')
    return '\n'.join(lines)


def build_part1(btc: dict) -> str:
    return '\n'.join([
        render_hl_block(),
        '',
        '**Section 2 OKX**',
        render_okx_block(),
        '',
        '**Section 3 市场综述（BTC）**',
        '',
        format_coin_section(btc),
    ])


def build_part2(eth: dict, btc: dict) -> str:
    return '\n'.join([
        '**Section 3 市场综述（ETH）**',
        '',
        format_coin_section(eth),
        '',
        render_shared_context(eth),
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
