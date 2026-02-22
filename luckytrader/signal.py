"""
Lucky Trading Signal System v5.1
单策略系统：放量突破
回测验证：104天30分钟K线，230笔交易，胜率54.8%，期望+1.02%/笔

参数（全量优化，1015组合扫描，next_open入场）：
- 入场：突破24h区间 + 放量确认（可配置窗口）
- 止损：4%
- 止盈：7%
- 持仓上限：60h
"""
from hyperliquid.info import Info
import time
from datetime import datetime, timezone
from luckytrader.config import get_config

def get_candles(coin, interval, hours):
    info = Info(skip_ws=True)
    end = int(time.time() * 1000)
    start = end - hours * 3600 * 1000
    return info.candles_snapshot(coin, interval, start, end)

def ema(data, period):
    result = [data[0]]
    k = 2 / (period + 1)
    for i in range(1, len(data)):
        result.append(data[i] * k + result[-1] * (1 - k))
    return result

def rsi(data, period=14):
    result = [50] * period
    for i in range(period, len(data)):
        gains, losses = [], []
        for j in range(i - period + 1, i + 1):
            change = data[j] - data[j-1]
            if change > 0: gains.append(change)
            elif change < 0: losses.append(abs(change))
        avg_gain = sum(gains) / period if gains else 0
        avg_loss = sum(losses) / period if losses else 0.0001
        if avg_loss == 0:
            avg_loss = 0.0001
        rs = avg_gain / avg_loss
        result.append(100 - 100 / (1 + rs))
    return result

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
    """获取最近成交"""
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

def analyze(coin='BTC'):
    candles_1h = get_candles(coin, '1h', 72)
    _cfg_strategy = get_config().strategy
    _lookback = _cfg_strategy.lookback_bars
    _range = _cfg_strategy.range_bars
    candles_30m = get_candles(coin, '30m', max(48, _range + 2))
    
    if not candles_1h or len(candles_1h) < 50:
        return {"error": "数据不足"}
    
    result = {}
    
    # 市场上下文（资金费率、OI、ETH）
    result['market_context'] = get_market_context()
    result['recent_fills'] = get_recent_fills(3)
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
    vol_confirm = vol_ratio_30m > _cfg.strategy.vol_threshold
    
    result['breakout'] = {
        'up': breakout_up,
        'down': breakout_down,
        'vol_ratio_30m': vol_ratio_30m,
        'vol_confirm': vol_confirm,
    }
    
    # 4h 趋势方向过滤（顺势交易，回测验证：期望提升22-38%）
    candles_4h = get_candles(coin, '4h', 21 * 4)  # 最近21根4h K线
    trend_4h = 'UNKNOWN'
    if candles_4h and len(candles_4h) >= 21:
        closes_4h = [float(c['c']) for c in candles_4h]
        ema8_4h = ema(closes_4h, 8)
        ema21_4h = ema(closes_4h, 21)
        trend_4h = 'UP' if ema8_4h[-1] > ema21_4h[-1] else 'DOWN'
    result['trend_4h'] = trend_4h

    if breakout_up and vol_confirm:
        if trend_4h == 'DOWN':
            result['signal'] = 'HOLD'
            result['signal_reasons'] = []
            result['signal_filtered'] = f'LONG信号被过滤（4h趋势=DOWN，逆势不入场）'
        else:
            result['signal'] = 'LONG'
            result['signal_reasons'] = [f'突破区间高点${high_range:,.0f}', f'放量{vol_ratio_30m:.1f}x', f'4h趋势{trend_4h}']
    elif breakout_down and vol_confirm:
        if trend_4h == 'UP':
            result['signal'] = 'HOLD'
            result['signal_reasons'] = []
            result['signal_filtered'] = f'SHORT信号被过滤（4h趋势=UP，逆势不入场）'
        else:
            result['signal'] = 'SHORT'
            result['signal_reasons'] = [f'跌破区间低点${low_range:,.0f}', f'放量{vol_ratio_30m:.1f}x', f'4h趋势{trend_4h}']
    else:
        result['signal'] = 'HOLD'
        result['signal_reasons'] = []
    
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
    
    # 最近成交
    fills = result.get('recent_fills', [])
    if fills:
        from datetime import datetime, timezone, timedelta
        _CST = timezone(timedelta(hours=8))
        lines.append(f"\n📋 最近成交:")
        for f in fills:
            t = datetime.fromtimestamp(f['time']/1000, tz=timezone.utc).astimezone(_CST).strftime('%m-%d %H:%M')
            lines.append(f"  {t} | {f['coin']} {f['side']} {f['size']} @ ${float(f['price']):,.0f}")
    
    return '\n'.join(lines)

if __name__ == '__main__':
    import sys
    coin = sys.argv[1] if len(sys.argv) > 1 else 'BTC'
    result = analyze(coin)
    print(format_report(result))
