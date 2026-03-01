#!/usr/bin/env python3
"""
BTC K线图生成器
生成最近 48 根 30m K线的蜡烛图，标注支撑/阻力位、EMA、当前持仓
输出 PNG 文件路径
"""
import logging
from luckytrader.strategy import ema  # 单一真相源：指标只在 strategy.py 定义
import matplotlib
matplotlib.use('Agg')  # 无头模式
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

logger = logging.getLogger(__name__)
from matplotlib.patches import FancyBboxPatch
from datetime import datetime, timezone, timedelta
import tempfile
import os
from pathlib import Path

from hyperliquid.info import Info
from luckytrader.signal import analyze
from luckytrader.config import get_config

# 颜色方案（暗色主题）
BG_COLOR = '#1a1a2e'
GRID_COLOR = '#2a2a4a'
UP_COLOR = '#00d26a'
DOWN_COLOR = '#f45b69'
EMA8_COLOR = '#ffd700'
EMA21_COLOR = '#87ceeb'
SUPPORT_COLOR = '#00d26a'
RESIST_COLOR = '#f45b69'
VOLUME_UP = '#00d26a55'
VOLUME_DOWN = '#f45b6955'
TEXT_COLOR = '#e0e0e0'
ENTRY_COLOR = '#ffa500'
MACD_COLOR = '#ffd700'
SIGNAL_COLOR = '#ff6b9d'
HIST_UP_COLOR = '#00d26a88'
HIST_DOWN_COLOR = '#f45b6988'


def compute_macd(closes, fast=12, slow=26, signal=9):
    """计算 MACD (fast EMA - slow EMA), signal line, histogram"""
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_line = ema(macd_line, signal)
    histogram = [m - s for m, s in zip(macd_line, signal_line)]
    return macd_line, signal_line, histogram


def get_candles_raw(coin='BTC', interval='30m', count=60):
    """获取原始 K 线数据"""
    info = Info(skip_ws=True)
    import time
    end = int(time.time() * 1000)
    start = end - count * 30 * 60 * 1000  # 30m per candle
    return info.candles_snapshot(coin, interval, start, end)





def generate_chart(coin='BTC', output_path=None, position=None, signal_result=None):
    """
    生成 K 线图
    
    Args:
        coin: 交易对
        output_path: 输出路径，None 则自动生成临时文件
        position: 当前持仓 dict (entry_price, direction) 或 None
        signal_result: analyze() 的结果，如果已有则不重新获取
    
    Returns:
        str: 图片文件路径
    """
    # 获取数据
    candles = get_candles_raw(coin, '30m', 60)
    if not candles or len(candles) < 20:
        return None
    
    # 只取最后 48 根显示
    candles = candles[-48:]
    
    # 解析数据
    times = [datetime.fromtimestamp(int(c['t']) / 1000, tz=timezone.utc) for c in candles]
    opens = [float(c['o']) for c in candles]
    highs = [float(c['h']) for c in candles]
    lows = [float(c['l']) for c in candles]
    closes = [float(c['c']) for c in candles]
    volumes = [float(c['v']) * float(c['c']) for c in candles]
    
    # 布林带（用更多数据让前几根也准确）
    all_candles = get_candles_raw(coin, '30m', 80)
    all_closes = [float(c['c']) for c in all_candles]
    bb_period = 20
    bb_std = 2.0
    bb_mid_full, bb_upper_full, bb_lower_full = [], [], []
    for i in range(len(all_closes)):
        if i < bb_period - 1:
            bb_mid_full.append(all_closes[i])
            bb_upper_full.append(all_closes[i])
            bb_lower_full.append(all_closes[i])
        else:
            window = all_closes[i - bb_period + 1:i + 1]
            mid = sum(window) / bb_period
            std = (sum((x - mid) ** 2 for x in window) / bb_period) ** 0.5
            bb_mid_full.append(mid)
            bb_upper_full.append(mid + bb_std * std)
            bb_lower_full.append(mid - bb_std * std)
    # 对齐到最后 48 根
    offset = len(all_candles) - len(candles)
    bb_mid = bb_mid_full[offset:]
    bb_upper = bb_upper_full[offset:]
    bb_lower = bb_lower_full[offset:]
    
    # 获取支撑阻力（如果没传入 signal_result）
    supports = []
    resistances = []
    if signal_result:
        supports = signal_result.get('supports', [])
        resistances = signal_result.get('resistances', [])
    else:
        try:
            result = analyze(coin)
            supports = result.get('supports', [])
            resistances = result.get('resistances', [])
        except Exception as e:
            logger.warning(f"Failed to get support/resistance levels: {e}")
    
    # 获取持仓信息（per-coin）
    if position is None:
        try:
            from luckytrader.execute import load_state
            state = load_state(coin)
            if state.get('position'):
                position = state['position']
        except Exception as e:
            logger.warning(f"Failed to load position state for chart: {e}")
            pass
    
    # MACD（用更多数据避免前几根不准）
    macd_line_full, signal_line_full, histogram_full = compute_macd(all_closes)
    macd_line = macd_line_full[offset:]
    signal_line = signal_line_full[offset:]
    histogram = histogram_full[offset:]
    
    # ====== 绘图 ======
    fig, (ax1, ax_macd, ax2) = plt.subplots(3, 1, figsize=(14, 9),
                                     gridspec_kw={'height_ratios': [3.5, 1.2, 1]},
                                     facecolor=BG_COLOR)
    fig.subplots_adjust(hspace=0.08, left=0.08, right=0.95, top=0.92, bottom=0.08)
    
    ax1.set_facecolor(BG_COLOR)
    ax_macd.set_facecolor(BG_COLOR)
    ax2.set_facecolor(BG_COLOR)
    
    # K线
    width = timedelta(minutes=20)
    thin_width = timedelta(minutes=3)
    
    for i in range(len(candles)):
        color = UP_COLOR if closes[i] >= opens[i] else DOWN_COLOR
        # 实体
        body_low = min(opens[i], closes[i])
        body_high = max(opens[i], closes[i])
        body_height = max(body_high - body_low, (highs[i] - lows[i]) * 0.005)  # 最小可见高度
        ax1.bar(times[i], body_height, width=width, bottom=body_low,
                color=color, edgecolor=color, linewidth=0.5)
        # 影线
        ax1.bar(times[i], highs[i] - body_high, width=thin_width,
                bottom=body_high, color=color, linewidth=0)
        ax1.bar(times[i], body_low - lows[i], width=thin_width,
                bottom=lows[i], color=color, linewidth=0)
    
    # 布林带
    BB_MID_COLOR = '#ffd700'
    BB_BAND_COLOR = '#87ceeb'
    ax1.plot(times, bb_mid, color=BB_MID_COLOR, linewidth=1, alpha=0.8, label='BB Mid')
    ax1.plot(times, bb_upper, color=BB_BAND_COLOR, linewidth=0.8, alpha=0.6, label='BB Upper')
    ax1.plot(times, bb_lower, color=BB_BAND_COLOR, linewidth=0.8, alpha=0.6, label='BB Lower')
    ax1.fill_between(times, bb_lower, bb_upper, color=BB_BAND_COLOR, alpha=0.08)
    
    # 支撑阻力位（只画前2个最强的）
    price_min = min(lows)
    price_max = max(highs)
    price_range = price_max - price_min
    
    for i, (level, count) in enumerate(supports[:2]):
        if price_min - price_range * 0.05 < level < price_max + price_range * 0.05:
            ax1.axhline(y=level, color=SUPPORT_COLOR, linestyle='--', linewidth=0.8, alpha=0.6)
            ax1.text(times[-1] + timedelta(minutes=10), level, f'S ${level:,.0f}',
                    color=SUPPORT_COLOR, fontsize=7, va='center', alpha=0.8)
    
    for i, (level, count) in enumerate(resistances[:2]):
        if price_min - price_range * 0.05 < level < price_max + price_range * 0.05:
            ax1.axhline(y=level, color=RESIST_COLOR, linestyle='--', linewidth=0.8, alpha=0.6)
            ax1.text(times[-1] + timedelta(minutes=10), level, f'R ${level:,.0f}',
                    color=RESIST_COLOR, fontsize=7, va='center', alpha=0.8)
    
    # 持仓标注
    if position and position.get('entry_price'):
        entry = position['entry_price']
        direction = position.get('direction', '')
        color = UP_COLOR if direction == 'LONG' else DOWN_COLOR
        ax1.axhline(y=entry, color=ENTRY_COLOR, linestyle=':', linewidth=1, alpha=0.8)
        label = f'{"▲" if direction == "LONG" else "▼"} ${entry:,.0f}'
        ax1.text(times[0] - timedelta(minutes=10), entry, label,
                color=ENTRY_COLOR, fontsize=7, va='center', ha='right', fontweight='bold')
        
        # SL/TP
        sl = position.get('sl_price')
        tp = position.get('tp_price')
        if sl:
            ax1.axhline(y=sl, color=DOWN_COLOR, linestyle=':', linewidth=0.7, alpha=0.5)
            ax1.text(times[-1] + timedelta(minutes=10), sl, f'SL ${sl:,.0f}',
                    color=DOWN_COLOR, fontsize=6, va='center', alpha=0.7)
        if tp:
            ax1.axhline(y=tp, color=UP_COLOR, linestyle=':', linewidth=0.7, alpha=0.5)
            ax1.text(times[-1] + timedelta(minutes=10), tp, f'TP ${tp:,.0f}',
                    color=UP_COLOR, fontsize=6, va='center', alpha=0.7)
    
    # MACD 子图
    ax_macd.plot(times, macd_line, color=MACD_COLOR, linewidth=1, label='MACD')
    ax_macd.plot(times, signal_line, color=SIGNAL_COLOR, linewidth=1, label='Signal')
    ax_macd.axhline(y=0, color=GRID_COLOR, linewidth=0.5)
    for i in range(len(times)):
        color = HIST_UP_COLOR if histogram[i] >= 0 else HIST_DOWN_COLOR
        ax_macd.bar(times[i], histogram[i], width=width, color=color)
    ax_macd.set_ylabel('MACD', color=TEXT_COLOR, fontsize=8)
    ax_macd.legend(loc='upper left', fontsize=6, facecolor=BG_COLOR, edgecolor=GRID_COLOR,
                   labelcolor=TEXT_COLOR)
    
    # 成交量
    for i in range(len(candles)):
        color = VOLUME_UP if closes[i] >= opens[i] else VOLUME_DOWN
        ax2.bar(times[i], volumes[i], width=width, color=color)
    
    # 格式化
    ax1.set_ylabel('Price (USD)', color=TEXT_COLOR, fontsize=8)
    ax2.set_ylabel('Vol', color=TEXT_COLOR, fontsize=8)
    
    # 当前价格标注
    current = closes[-1]
    prev = closes[-2] if len(closes) > 1 else current
    change_pct = (current - prev) / prev * 100
    price_color = UP_COLOR if current >= prev else DOWN_COLOR
    
    title = f'{coin}/USD  30m  ${current:,.0f}  ({change_pct:+.2f}%)'
    ax1.set_title(title, color=price_color, fontsize=11, fontweight='bold', pad=8)
    
    # 图例
    ax1.legend(loc='upper left', fontsize=7, facecolor=BG_COLOR, edgecolor=GRID_COLOR,
              labelcolor=TEXT_COLOR)
    
    # 网格和轴
    for ax in [ax1, ax_macd, ax2]:
        ax.grid(True, color=GRID_COLOR, linewidth=0.3, alpha=0.5)
        ax.tick_params(colors=TEXT_COLOR, labelsize=7)
        ax.spines['top'].set_visible(False)
        ax.spines['right'].set_visible(False)
        ax.spines['bottom'].set_color(GRID_COLOR)
        ax.spines['left'].set_color(GRID_COLOR)
    
    ax1.tick_params(labelbottom=False)
    ax_macd.tick_params(labelbottom=False)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d %H:%M', tz=timezone.utc))
    plt.setp(ax2.xaxis.get_majorticklabels(), rotation=30, ha='right')
    
    # Y 轴价格格式
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, p: f'${x:,.0f}'))
    ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, p: f'${x/1e6:.0f}M' if x >= 1e6 else f'${x/1e3:.0f}K'))
    
    # 自动 y 轴范围（给支撑阻力留空间）
    margin = price_range * 0.05
    ax1.set_ylim(price_min - margin, price_max + margin)
    
    # 输出
    if output_path is None:
        chart_dir = Path.home() / '.openclaw/workspace/logs/charts'
        chart_dir.mkdir(parents=True, exist_ok=True)
        output_path = str(chart_dir / f'{coin.lower()}_30m_{datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")}.png')
    
    fig.savefig(output_path, dpi=150, facecolor=BG_COLOR)
    plt.close(fig)
    
    # 无损压缩 PNG（oxipng -o4 约省 25%）
    import subprocess as _sp
    try:
        _sp.run(['oxipng', '-o', '4', '--strip', 'safe', '-q', output_path],
                timeout=10, check=False)
    except FileNotFoundError:
        logger.debug("oxipng not installed, skipping PNG optimization")
    
    return output_path


def send_chart_to_discord(image_path: str, caption: str = "📊 30m K线",
                          channel_id: str = None):
    """通过 Spacebar REST API 直接发送图片到 Discord 频道"""
    import subprocess, json
    
    if channel_id is None:
        cfg = get_config()
        channel_id = cfg.notifications.discord_channel_id
    
    # 读取 bot token
    config_path = Path.home() / '.openclaw/openclaw.json'
    if not config_path.exists():
        print(f"Config not found: {config_path}")
        return False
    
    with open(config_path) as f:
        oc_config = json.load(f)
    
    token = oc_config.get('channels', {}).get('discord', {}).get('token', '')
    if not token:
        print("Discord token not found in config")
        return False
    
    api_base = "https://chat.llbrother.org/api/v10"
    
    result = subprocess.run([
        'curl', '-s', '-X', 'POST',
        f'{api_base}/channels/{channel_id}/messages',
        '-H', f'Authorization: Bot {token}',
        '-F', f'payload_json={{"content":"{caption}"}}',
        '-F', f'file=@{image_path}',
    ], capture_output=True, text=True, timeout=30)
    
    try:
        resp = json.loads(result.stdout)
        if resp.get('id'):
            print(f"Chart sent: message {resp['id']}")
            return True
        else:
            print(f"Send failed: {resp}")
            return False
    except Exception as e:
        print(f"Send error: {e}, stdout: {result.stdout[:200]}")
        return False


if __name__ == '__main__':
    import sys, json, pathlib
    from luckytrader.config import TRADING_COINS
    send = '--send' in sys.argv

    # 支持 --coin BTC 指定单个币种，默认全部
    coins = TRADING_COINS
    for i, arg in enumerate(sys.argv):
        if arg == '--coin' and i + 1 < len(sys.argv):
            coins = [sys.argv[i + 1].upper()]

    # 读取持仓状态（per-coin）
    pos_states = {}
    try:
        from luckytrader.config import get_workspace_dir
        state_path = get_workspace_dir() / 'memory' / 'trading' / 'position_state.json'
        all_state = json.loads(state_path.read_text())
        # 新格式: {"BTC": {"position": ...}, "ETH": {"position": ...}}
        # 旧格式: {"position": ...}
        if "position" in all_state and not any(c in all_state for c in TRADING_COINS):
            # 旧格式
            pos = all_state.get("position")
            if pos:
                pos_states[pos.get("coin", "BTC")] = pos
        else:
            for c in TRADING_COINS:
                coin_state = all_state.get(c, {})
                if coin_state.get("position"):
                    pos_states[c] = coin_state["position"]
    except Exception as e:
        logger.warning(f"Failed to load position for chart CLI: {e}")

    for coin in coins:
        pos_data = pos_states.get(coin)
        path = generate_chart(coin=coin, position=pos_data)
        if path:
            print(f'{coin} chart saved: {path}')
            if send:
                send_chart_to_discord(path, caption=f"📊 {coin} 30m K线")
        else:
            print(f'Failed to generate {coin} chart')
