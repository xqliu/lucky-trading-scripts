#!/usr/bin/env python3
from __future__ import annotations

import io
import json
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / 'repos' / 'lucky-trading-scripts'))

from luckytrader.signal import analyze
from hyperliquid.info import Info
from okx_bb.config import load_config as load_okx_config
from okx_bb.executor import BBExecutor


def fmt_money(v: float, digits: int = 2) -> str:
    return f"${v:.{digits}f}"


def get_hl_account():
    info = Info(skip_ws=True)
    state = info.user_state(load_hl_wallet())
    return state


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

    state = get_hl_account()
    acct_val = float(state['marginSummary']['accountValue'])
    withdraw = float(state['withdrawable'])
    asset_positions = state.get('assetPositions', [])
    eth_pos = None
    for ap in asset_positions:
        pos = ap.get('position', {})
        if pos.get('coin') == 'ETH' and float(pos.get('szi', 0)) != 0:
            eth_pos = pos
            break

    lines = []
    lines.append('**30分钟市场报告**')
    lines.append('')
    lines.append('**Section 1 HL**')
    lines.append(f'- 账户权益：{fmt_money(acct_val)}')
    lines.append(f'- 可提现吗：{fmt_money(withdraw)}')
    if eth_pos:
        entry = float(eth_pos.get('entryPx') or 0)
        szi = abs(float(eth_pos.get('szi') or 0))
        pos_val = float(eth_pos.get('positionValue') or 0)
        upl = float(eth_pos.get('unrealizedPnl') or 0)
        roe = float(eth_pos.get('returnOnEquity') or 0) * 100
        lev = eth_pos.get('leverage', {}).get('value', '?')
        lines.append(f'- 当前持仓：ETH 多头 {szi}')
        lines.append(f'- 开仓价：{entry}')
        lines.append(f'- 当前仓位价值：{fmt_money(pos_val)}')
        lines.append(f'- 未实现盈亏：{fmt_money(upl)}')
        lines.append(f'- ROE：{roe:.2f}%')
        lines.append(f'- 保证金占用：{fmt_money(pos_val/float(lev) if str(lev).replace(".", "", 1).isdigit() and float(lev) != 0 else 0)}')
    else:
        lines.append('- 当前持仓：无')
    lines.append('')
    lines.append('**Section 2 OKX**')
    lines.append(render_okx_block())
    lines.append('')
    lines.append('**Section 3 市场综述**')
    lines.append(f"- BTC：{fmt_money(btc['price'])}，30m 趋势 {btc['trend']}，4h 趋势 {btc.get('trend_4h','N/A')}，RSI {btc['rsi']:.1f}。")
    lines.append(f"- ETH：{fmt_money(eth['price'])}，30m 趋势 {eth['trend']}，4h 趋势 {eth.get('trend_4h','N/A')}，RSI {eth['rsi']:.1f}。")
    lines.append(f"- BTC 区间：{fmt_money(btc['low_24h'])} - {fmt_money(btc['high_24h'])}，量比 {btc['volume_ratio']:.2f}x。")
    if eth_pos:
        lines.append('- HL 当前仍有 ETH 小仓位，继续按既定风控观察，不追新单。')
    else:
        lines.append('- HL 当前无仓位，等待新信号。')

    print('\n'.join(lines))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
