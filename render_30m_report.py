#!/usr/bin/env python3
from __future__ import annotations

import io
import json
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


def load_trailing_state() -> dict:
    path = Path(__file__).parent.parent / 'memory' / 'trading' / 'trailing_state.json'
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def yesno(v: bool) -> str:
    return 'Y' if v else 'N'


def sig_summary(r: dict) -> str:
    b = r.get('breakout', {})
    return (
        f"{r['coin']} ${r['price']:.0f} | 30m {r['trend']} / 4h {r.get('trend_4h','N/A')} | "
        f"RSI {r['rsi']:.1f} | 量比 {r['volume_ratio']:.2f}x | "
        f"↑突 {yesno(b.get('up', False))} ↓破 {yesno(b.get('down', False))} | 信号 {r['signal']}"
    )


def main() -> int:
    btc = analyze('BTC')
    eth = analyze('ETH')
    trailing = load_trailing_state()

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
        eth_ts = trailing.get('ETH', {})
        stop_text = '止损单在场' if eth_ts.get('has_stop') else '无止损单'
        if eth_ts.get('last_stop_price'):
            stop_text = f"SL {float(eth_ts['last_stop_price']):.0f}"
        trail_text = '追踪开' if eth_ts.get('trailing_active') else '追踪未开'
        hl_line = (
            f"HL: ${acct_val:.2f} | ETH LONG {abs(float(eth_pos['szi'])):g} @ {float(eth_pos['entryPx']):.0f} "
            f"| uPnL {float(eth_pos['unrealizedPnl']):+.2f} | {stop_text} | {trail_text}"
        )
    else:
        hl_line = f"HL: ${acct_val:.2f} | 空仓"

    okx_block = render_okx_block()
    if '当前持仓**: 无' in okx_block or '**当前持仓**: 无' in okx_block:
        if '本地残留' in okx_block:
            okx_line = 'OKX: 空仓 | Monitor active | 异常=本地残留'
        else:
            okx_line = 'OKX: 空仓 | Monitor active | 状态一致'
    elif '当前持仓' in okx_block:
        okx_line = 'OKX: 有持仓 | Monitor active | 需看图确认方向'
    else:
        okx_line = 'OKX: 状态未知'

    if btc['signal'] == 'HOLD' and eth['signal'] == 'HOLD':
        concl = '结论: BTC/ETH 都没形成可执行突破，继续等。'
    elif btc['signal'] != 'HOLD' or eth['signal'] != 'HOLD':
        concl = f"结论: 出现信号 — BTC {btc['signal']} / ETH {eth['signal']}，但仍按风控执行。"
    else:
        concl = '结论: 继续观察。'

    print('30分钟报告')
    print(hl_line)
    print(okx_line)
    print(sig_summary(btc))
    print(sig_summary(eth))
    print(
        f"区间: BTC {btc['low_24h']:.0f}-{btc['high_24h']:.0f} | "
        f"ETH {eth['low_24h']:.0f}-{eth['high_24h']:.0f}"
    )
    print(concl)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
