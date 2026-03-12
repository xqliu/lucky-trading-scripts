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


def main() -> int:
    btc = analyze('BTC')
    eth = analyze('ETH')

    info = Info(skip_ws=True)
    state = info.user_state(load_hl_wallet())
    acct_val = float(state['marginSummary']['accountValue'])

    eth_pos = None
    for ap in state.get('assetPositions', []):
        pos = ap.get('position', {})
        if pos.get('coin') == 'ETH' and float(pos.get('szi', 0)) != 0:
            eth_pos = pos
            break

    if eth_pos:
        hl_line = (
            f"HL: ${acct_val:.2f} | ETH LONG {abs(float(eth_pos['szi'])):g} @ {float(eth_pos['entryPx']):.0f} "
            f"| uPnL {float(eth_pos['unrealizedPnl']):+.2f}"
        )
    else:
        hl_line = f"HL: ${acct_val:.2f} | 空仓"

    okx_block = render_okx_block()
    if '当前持仓**: 无' in okx_block or '**当前持仓**: 无' in okx_block:
        if '本地残留' in okx_block:
            okx_line = 'OKX: 空仓 | Monitor active | 本地残留待清理'
        else:
            okx_line = 'OKX: 空仓 | Monitor active'
    elif '当前持仓' in okx_block:
        okx_line = 'OKX: 有持仓 | Monitor active'
    else:
        okx_line = 'OKX: 状态未知'

    print('30分钟报告')
    print(hl_line)
    print(okx_line)
    print(
        f"BTC: ${btc['price']:.0f} | 30m {btc['trend']} / 4h {btc.get('trend_4h','N/A')} | "
        f"RSI {btc['rsi']:.1f} | 量比 {btc['volume_ratio']:.2f}x"
    )
    print(
        f"ETH: ${eth['price']:.0f} | 30m {eth['trend']} / 4h {eth.get('trend_4h','N/A')} | "
        f"RSI {eth['rsi']:.1f}"
    )
    print('结论: 震荡市，按既定风控观察，不追新单。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
