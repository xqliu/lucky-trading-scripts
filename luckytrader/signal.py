"""
Lucky Trading Signal System v6.0
单策略系统：放量突破

核心逻辑统一在 strategy.py — 本文件只负责：
1. 从 API 获取数据
2. 调用 strategy.detect_signal() 生成信号
3. 组装报告（展示用字段）
"""
from hyperliquid.info import Info
import time
from datetime import datetime, timezone
from luckytrader.config import get_config, get_coin_config, TradingConfig
from luckytrader.strategy import ema, rsi, detect_signal, get_trend_4h, get_range_levels, get_vol_ratio

def get_candles(coin, interval, hours):
    info = Info(skip_ws=True)
    end = int(time.time() * 1000)
    start = end - hours * 3600 * 1000
    return info.candles_snapshot(coin, interval, start, end)

def get_market_context():
    """获取资金费率、OI、ETH数据"""
    import requests
    url = 'https://api.hyperliquid.xyz/info'
    try:
        resp = requests.post(url, json={'type': 'metaAndAssetCtxs'}, timeout=10)
        data = resp.json()
        meta = data[0]['universe']
        ctxs = data[1]
        
        context = {}
        for i, asset in enumerate(meta):
            if asset['name'] in ('BTC', 'ETH'):
                ctx = ctxs[i]
                context[asset['name']] = {
                    'funding_rate': float(ctx['funding']),
                    'open_interest': float(ctx['openInterest']),
                    'mark_price': float(ctx['markPx']),
                }
        return context
    except:
        return {}

def get_recent_fills(limit=3):
    """获取最近成交（原始 fills，保留供其他模块使用）"""
    import requests
    url = 'https://api.hyperliquid.xyz/info'
    wallet = get_config().exchange.main_wallet
    try:
        resp = requests.post(url, json={'type': 'userFills', 'user': wallet}, timeout=10)
        fills = resp.json()[:limit]
        return [{
            'coin': f['coin'],
            'side': 'BUY' if f['side'] == 'B' else 'SELL',
            'size': f['sz'],
            'price': f['px'],
            'time': int(f['time']),
        } for f in fills]
    except:
        return []

def get_recent_trades(limit=3):
    """获取最近 N 笔完整交易（开仓+平仓配对为一行）"""
    import requests
    url = 'https://api.hyperliquid.xyz/info'
    wallet = get_config().exchange.main_wallet
    try:
        resp = requests.post(url, json={'type': 'userFills', 'user': wallet}, timeout=10)
        raw = resp.json()[:30]  # 多取一些以便配对
    except:
        return []

    # 解析每条 fill
    fills = []
    for f in raw:
        fills.append({
            'coin': f['coin'],
            'side': 'BUY' if f['side'] == 'B' else 'SELL',
            'size': float(f['sz']),
            'price': float(f['px']),
            'time': int(f['time']),
            'dir': f.get('dir', ''),           # "Open Long/Short" or "Close Long/Short"
            'pnl': float(f.get('closedPnl', 0)),
        })

    # 配对逻辑：从新到旧遍历，Close 找对应的 Open
    trades = []
    used = set()
    for i, f in enumerate(fills):
        if i in used:
            continue
        is_close = f['dir'].startswith('Close')
        is_open = f['dir'].startswith('Open')

        if is_close:
            # 找对应的 Open（往后找，同 coin，相反方向）
            open_side = 'BUY' if f['side'] == 'SELL' else 'SELL'
            paired = None
            for j in range(i + 1, len(fills)):
                if j in used:
                    continue
                o = fills[j]
                if o['coin'] == f['coin'] and o['side'] == open_side and o['dir'].startswith('Open'):
                    paired = o
                    used.add(j)
                    break
            used.add(i)
            direction = 'LONG' if f['side'] == 'SELL' else 'SHORT'
            trades.append({
                'coin': f['coin'],
                'direction': direction,
                'open_price': paired['price'] if paired else None,
                'open_time': paired['time'] if paired else None,
                'close_price': f['price'],
                'close_time': f['time'],
                'pnl': f['pnl'],
                'status': 'closed',
            })
        elif is_open:
            # 开仓但无对应平仓（持仓中）
            used.add(i)
            direction = 'LONG' if f['side'] == 'BUY' else 'SHORT'
            trades.append({
                'coin': f['coin'],
                'direction': direction,
                'open_price': f['price'],
                'open_time': f['time'],
                'close_price': None,
                'close_time': None,
                'pnl': None,
                'status': 'open',
            })

        if len(trades) >= limit:
            break

    return trades

def analyze(coin='BTC'):
    candles_1h = get_candles(coin, '1h', 72)
    # Per-coin config with test-friendly fallback
    _cfg_fallback = get_config()
    _coin_cfg = None
    # Only apply real per-coin overrides when using the real TradingConfig object.
    # In unit tests get_config is often patched to MagicMock; keep test-provided params.
    if isinstance(_cfg_fallback, TradingConfig):
        try:
            _coin_cfg = get_coin_config(coin)
        except Exception:
            _coin_cfg = None

    _lookback = getattr(_coin_cfg, 'lookback_bars', _cfg_fallback.strategy.lookback_bars)
    _range = getattr(_coin_cfg, 'range_bars', _cfg_fallback.strategy.range_bars)
    # 30m K线：至少需要 (_range + 2) 根bar，每根30m = 0.5h
    # 请求 (_range + 2) / 2 + 24h 额外余量，确保有足够数据
    _30m_hours_needed = (_range + 2) // 2 + 24
    candles_30m = get_candles(coin, '30m', max(48, _30m_hours_needed))
    
    if not candles_1h or len(candles_1h) < 50:
        return {"error": "数据不足"}
    
    result = {}
    
    # 市场上下文（资金费率、OI、ETH）
    result['market_context'] = get_market_context()
    result['recent_trades'] = get_recent_trades(3)
    closes = [float(c['c']) for c in candles_1h]
    volumes = [float(c['v']) * float(c['c']) for c in candles_1h]
    
    # 当前价格
    current_price = closes[-1]
    result['price'] = current_price
    
    # Price range detection (configurable window)
    # range_slice 必须排除突破判定用的 candles_30m[-2]，否则突破 K 线自身
    # 定义了区间边界，导致 breakout_down/up 永远为 False
    if candles_30m and len(candles_30m) >= _range + 2:
        range_slice = candles_30m[-(_range+2):-2]  # N bars before the breakout candle
    else:
        range_slice = candles_30m[:-2] if candles_30m and len(candles_30m) > 2 else candles_1h[-25:-1]
    
    high_range = max(float(c['h']) for c in range_slice)
    low_range = min(float(c['l']) for c in range_slice)
    range_pct = (high_range - low_range) / low_range * 100
    result['high_24h'] = high_range  # keep key names for compatibility
    result['low_24h'] = low_range
    result['range_24h'] = range_pct
    
    # 技术指标 (用于报告展示，不影响信号)
    ema_8 = ema(closes, 8)
    ema_21 = ema(closes, 21)
    rsi_14 = rsi(closes, 14)
    
    result['ema_8'] = ema_8[-1]
    result['ema_21'] = ema_21[-1]
    result['rsi'] = rsi_14[-1]
    result['trend'] = 'UP' if ema_8[-1] > ema_21[-1] else 'DOWN'
    
    # 支撑/阻力（用1h K线近30天日线）
    candles_1d = get_candles(coin, '1d', 30 * 24)
    if candles_1d:
        daily_lows = [float(c['l']) for c in candles_1d]
        daily_highs = [float(c['h']) for c in candles_1d]
        result['supports'] = find_levels(daily_lows, current_price, 'support')
        result['resistances'] = find_levels(daily_highs, current_price, 'resistance')
    else:
        result['supports'] = []
        result['resistances'] = []
    
    # === 放量突破信号 ===
    # 用上一根已收盘的30m K线检测（避免未收盘K线成交量失真）
    # 突破判定用 high/low（盘中突破即算），回测验证总收益+50%
    if candles_30m and len(candles_30m) >= 3:
        latest_30m_high = float(candles_30m[-2]['h'])   # 上一根已收盘的最高价
        latest_30m_low = float(candles_30m[-2]['l'])    # 上一根已收盘的最低价
        latest_30m_vol = float(candles_30m[-2]['v']) * float(candles_30m[-2]['c'])
        # Volume average over configurable lookback window
        vol_start = max(0, len(candles_30m) - 2 - _lookback)
        vol_slice = candles_30m[vol_start:-2]
        avg_30m_vol = sum(float(c['v']) * float(c['c']) for c in vol_slice) / len(vol_slice) if vol_slice else 0
        vol_ratio_30m = latest_30m_vol / avg_30m_vol if avg_30m_vol > 0 else 0
    else:
        latest_30m_high = current_price
        latest_30m_low = current_price
        latest_30m_vol = 0
        avg_30m_vol = 0
        vol_ratio_30m = 0
    
    # 统一用30m成交量展示（和信号检测一致）
    result['volume_usd'] = latest_30m_vol
    result['avg_volume_24h'] = avg_30m_vol
    result['volume_ratio'] = vol_ratio_30m
    
    breakout_up = latest_30m_high > high_range
    breakout_down = latest_30m_low < low_range
    _cfg = get_config()
    vol_threshold = getattr(_coin_cfg, 'vol_threshold', _cfg.strategy.vol_threshold)
    vol_confirm = vol_ratio_30m > vol_threshold
    
    result['breakout'] = {
        'up': breakout_up,
        'down': breakout_down,
        'vol_ratio_30m': vol_ratio_30m,
        'vol_confirm': vol_confirm,
    }
    
    # 4h 趋势 — 通过 strategy.get_trend_4h()（统一逻辑）
    # 需要足够的 4h K线用于 trend EMA 计算
    _trend_ema = getattr(_coin_cfg, 'trend_ema_period', 0)
    _4h_hours = max(42, _trend_ema + 10) * 4 if _trend_ema > 0 else 42 * 4
    candles_4h = get_candles(coin, '4h', _4h_hours)
    trend_4h = get_trend_4h(candles_4h, int(time.time() * 1000), _trend_ema)
    result['trend_4h'] = trend_4h

    # 信号判断 — 通过 strategy.detect_signal()（统一逻辑）
    # idx = len(candles_30m) - 1 → 检查倒数第二根已收盘 K 线
    # Pass coin config as cfg — detect_signal reads strategy attrs from it
    signal = detect_signal(candles_30m, candles_4h, len(candles_30m) - 1, _cfg, _coin_cfg)
    
    if signal == 'LONG':
        result['signal'] = 'LONG'
        result['signal_reasons'] = [f'突破区间高点${high_range:,.0f}', f'放量{vol_ratio_30m:.1f}x', f'4h趋势{trend_4h}']
    elif signal == 'SHORT':
        result['signal'] = 'SHORT'
        result['signal_reasons'] = [f'跌破区间低点${low_range:,.0f}', f'放量{vol_ratio_30m:.1f}x', f'4h趋势{trend_4h}']
    else:
        result['signal'] = 'HOLD'
        result['signal_reasons'] = []
        # 判断是否被过滤（有突破+放量但被4h趋势拦截）
        if breakout_up and vol_confirm and trend_4h == 'DOWN':
            result['signal_filtered'] = f'LONG信号被过滤（4h趋势=DOWN，逆势不入场）'
        elif breakout_down and vol_confirm and trend_4h == 'UP':
            result['signal_filtered'] = f'SHORT信号被过滤（4h趋势=UP，逆势不入场）'
    
    # 止损/止盈（回测最优参数）
    if result['signal'] == 'LONG':
        result['suggested_stop'] = round(current_price * (1 - _cfg.risk.stop_loss_pct))
        result['suggested_tp'] = round(current_price * (1 + _cfg.risk.take_profit_pct))
    elif result['signal'] == 'SHORT':
        result['suggested_stop'] = round(current_price * (1 + _cfg.risk.stop_loss_pct))
        result['suggested_tp'] = round(current_price * (1 - _cfg.risk.take_profit_pct))
    
    return result

def find_levels(prices, current, direction):
    levels = []
    for p in prices:
        if (direction == 'support' and p < current) or (direction == 'resistance' and p > current):
            nearby = sum(1 for pp in prices if abs(pp - p) / p < 0.02)
            if nearby >= 2:
                levels.append((p, nearby))
    if not levels: return []
    levels.sort(key=lambda x: x[0])
    clusters = []
    cur = [levels[0]]
    for i in range(1, len(levels)):
        if (levels[i][0] - cur[0][0]) / cur[0][0] < 0.02:
            cur.append(levels[i])
        else:
            clusters.append((round(sum(l[0] for l in cur)/len(cur), 1), sum(l[1] for l in cur)))
            cur = [levels[i]]
    if cur:
        clusters.append((round(sum(l[0] for l in cur)/len(cur), 1), sum(l[1] for l in cur)))
    return sorted(clusters, key=lambda x: -x[1])[:3]

def format_report(result):
    if 'error' in result:
        return result['error']
    
    lines = []
    lines.append(f"💰 价格: ${result['price']:,.0f}")
    lines.append(f"📊 成交量: ${result['volume_usd']:,.0f} (均值: ${result['avg_volume_24h']:,.0f}, {result['volume_ratio']:.2f}x)")
    lines.append(f"📏 区间: ${result['low_24h']:,.0f} - ${result['high_24h']:,.0f} ({result['range_24h']:.1f}%)")
    lines.append(f"📈 趋势: {result['trend']} (EMA8: {result['ema_8']:,.0f} / EMA21: {result['ema_21']:,.0f}) | 4h趋势: {result.get('trend_4h', 'N/A')}")
    lines.append(f"📉 RSI: {result['rsi']:.1f}")
    
    # 突破检测 - 分方向展示
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
        _c = get_config()
        lines.append(f"🛑 止损: ${result['suggested_stop']:,.0f} (-{_c.risk.stop_loss_pct*100:.0f}%)")
        lines.append(f"🎯 止盈: ${result['suggested_tp']:,.0f} (+{_c.risk.take_profit_pct*100:.0f}%)")
        lines.append(f"⏰ 持仓上限: {_c.risk.max_hold_hours}h")
    
    # 市场上下文
    ctx = result.get('market_context', {})
    if ctx:
        lines.append(f"\n💹 资金费率 & OI:")
        for coin_name in ('BTC', 'ETH'):
            c = ctx.get(coin_name)
            if c:
                fr = c['funding_rate']
                fr_annual = fr * 24 * 365 * 100
                oi_usd = c['open_interest'] * c['mark_price']
                lines.append(f"  {coin_name}: 费率 {fr*100:.4f}%/h ({fr_annual:+.1f}%年化) | OI ${oi_usd/1e9:.2f}B | ${c['mark_price']:,.0f}")
    
    # 最近交易（开仓+平仓配对）
    trades = result.get('recent_trades', [])
    if trades:
        from datetime import datetime, timezone, timedelta
        _CST = timezone(timedelta(hours=8))
        lines.append(f"\n📋 最近交易:")
        for t in trades:
            def _fmt_time(ts):
                return datetime.fromtimestamp(ts/1000, tz=timezone.utc).astimezone(_CST).strftime('%m-%d %H:%M')
            if t['status'] == 'closed' and t['open_price']:
                open_t = _fmt_time(t['open_time'])
                close_t = _fmt_time(t['close_time'])
                pnl_str = f" | {'+' if t['pnl'] >= 0 else ''}{t['pnl']:.2f}U" if t['pnl'] is not None else ""
                lines.append(f"  {t['coin']} {t['direction']} {open_t} {t['open_price']:,.0f}→{close_t} {t['close_price']:,.0f}{pnl_str}")
            elif t['status'] == 'open':
                open_t = _fmt_time(t['open_time'])
                lines.append(f"  {t['coin']} {t['direction']} {open_t} {t['open_price']:,.0f}→持仓中")
    
    return '\n'.join(lines)

if __name__ == '__main__':
    import sys
    coin = sys.argv[1] if len(sys.argv) > 1 else 'BTC'
    result = analyze(coin)
    print(format_report(result))
