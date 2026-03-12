#!/usr/bin/env python3
from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / 'repos' / 'lucky-trading-scripts'))

from hyperliquid.info import Info
from luckytrader.signal import analyze, format_report


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

    positions = []
    for ap in state.get('assetPositions', []):
        pos = ap.get('position', {})
        szi = float(pos.get('szi', 0) or 0)
        if szi == 0:
            continue
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
        positions.append(coin)

    if not positions:
        lines.append('- 当前持仓：无')

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
    parts = [
        render_hl_block(),
        '',
        '**Section 2 OKX**',
        render_okx_block(),
        '',
        '**Section 3 市场综述（BTC）**',
        '',
        format_report(btc),
    ]
    return '\n'.join(parts)


def build_part2(eth: dict, btc: dict) -> str:
    parts = [
        '**Section 3 市场综述（ETH）**',
        '',
        format_report(eth),
        '',
        render_conclusion(btc, eth),
    ]
    return '\n'.join(parts)


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
