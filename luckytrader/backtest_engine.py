#!/usr/bin/env python3
"""
回测引擎 — 复用实盘代码
========================
核心原则：回测和实盘使用**完全相同**的判断逻辑。

架构：
- signal.py 的信号判断逻辑 → 回测直接调用
- regime.py 的 DE/regime 判断 → 回测直接调用
- 本模块只负责：数据获取 + 模拟持仓管理 + 结果统计

不重写的东西：
- 信号生成（用 signal.py 的 analyze 逻辑）
- TP/SL 判断（和实盘一样按百分比）
- Regime 判断（用 regime.py）
- 动态 TP 收紧（和实盘 reeval_regime_tp 同逻辑）
"""
import sys
import time
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, List, Tuple

from hyperliquid.info import Info
from luckytrader.regime import compute_de, get_regime_params
from luckytrader.indicators import ema, rsi
from luckytrader.strategy import detect_signal, should_tighten_tp, check_exit, compute_tp_price
from luckytrader.config import get_config


def get_historical_candles(coin: str, interval: str, days: int) -> list:
    """获取历史 K 线"""
    info = Info(skip_ws=True)
    end = int(time.time() * 1000)
    start = end - days * 24 * 3600 * 1000
    return info.candles_snapshot(coin, interval, start, end)


    # detect_signal 已移到 strategy.py，回测和实盘共用同一份实现


def compute_de_for_date(candles_1d: list, bar_time_ms: int, lookback_days: int = 7) -> Optional[float]:
    """计算某个时间点的 DE"""
    # 找到 <= bar_time 的最近日线
    idx = len(candles_1d) - 1
    while idx >= 0 and int(candles_1d[idx]['t']) > bar_time_ms:
        idx -= 1
    if idx < lookback_days:
        return None
    window = candles_1d[idx - lookback_days:idx + 1]
    return compute_de(window, lookback_days)


class Position:
    """模拟持仓 — 退出判断委托给 strategy.check_exit()"""
    def __init__(self, direction: str, entry_price: float, entry_bar: int,
                 entry_time: str, tp_pct: float, sl_pct: float,
                 regime: str, de: float):
        self.direction = direction
        self.entry_price = entry_price
        self.entry_bar = entry_bar
        self.entry_time = entry_time
        self.tp_pct = tp_pct
        self.sl_pct = sl_pct
        self.regime = regime
        self.entry_regime = regime
        self.de = de
    
    def try_exit(self, price: float, bars_held: int, max_hold_bars: int) -> Optional[Tuple[str, float]]:
        """检查是否应该退出 — 调用 strategy.check_exit()"""
        result = check_exit(self.direction, self.entry_price, price,
                           bars_held, self.tp_pct, self.sl_pct, max_hold_bars)
        if result:
            reason, pnl = result
            # 附加 regime 信息到 reason
            return (f'{reason}({self.regime}:{self.tp_pct*100:.0f}%)', pnl)
        return None
    
    def update_regime(self, new_de: Optional[float], cfg) -> bool:
        """动态 regime 重估 — 调用 strategy.should_tighten_tp()"""
        new_tp = should_tighten_tp(self.tp_pct, new_de, cfg)
        if new_tp is None:
            return False
        self.tp_pct = new_tp
        self.regime = get_regime_params(new_de, cfg)['regime']
        return True


def run_backtest(coin: str = 'BTC', days: int = 90, dynamic_regime: bool = True,
                 verbose: bool = False) -> Dict:
    """
    统一回测引擎
    
    Args:
        coin: 交易对
        days: 回测天数
        dynamic_regime: 是否启用动态 regime TP 调整
        verbose: 是否打印每笔交易详情
    
    Returns:
        dict with trades list and summary stats
    """
    cfg = get_config()
    max_hold_bars = int(cfg.risk.max_hold_hours * 2)  # 30m bars
    
    print(f"📊 获取 {days} 天数据...")
    candles_30m = get_historical_candles(coin, '30m', days + 10)
    candles_4h = get_historical_candles(coin, '4h', days + 30)
    candles_1d = get_historical_candles(coin, '1d', days + 30)
    
    if not candles_30m or not candles_1d:
        print("❌ 数据获取失败")
        return {"trades": [], "error": "no data"}
    
    print(f"   30m: {len(candles_30m)} | 4h: {len(candles_4h)} | 1d: {len(candles_1d)}")
    
    trades = []
    position: Optional[Position] = None
    
    start_idx = max(cfg.strategy.range_bars, cfg.strategy.lookback_bars, 50) + 2
    
    for i in range(start_idx, len(candles_30m)):
        bar_time = int(candles_30m[i]['t'])
        dt = datetime.fromtimestamp(bar_time / 1000, tz=timezone.utc)
        price = float(candles_30m[i]['c'])
        
        # 持仓管理
        if position:
            bars_held = i - position.entry_bar
            
            # 动态 regime 重估（和实盘一样，每天检查）
            if dynamic_regime and bars_held > 0 and bars_held % 48 == 0:  # 每24h
                de = compute_de_for_date(candles_1d, bar_time, cfg.strategy.de_lookback_days)
                position.update_regime(de, cfg)
            
            # 检查退出
            exit_result = position.try_exit(price, bars_held, max_hold_bars)
            if exit_result:
                reason, pnl = exit_result
                trades.append({
                    'entry_time': position.entry_time,
                    'exit_time': dt.isoformat(),
                    'direction': position.direction,
                    'entry_price': position.entry_price,
                    'exit_price': price,
                    'pnl_pct': pnl * 100,
                    'reason': reason,
                    'bars_held': bars_held,
                    'entry_regime': position.entry_regime,
                    'exit_regime': position.regime,
                    'de': position.de,
                })
                position = None
        
        # 信号检测（无持仓时）
        if position is None:
            signal = detect_signal(candles_30m, candles_4h, i, cfg)
            if signal:
                de = compute_de_for_date(candles_1d, bar_time, cfg.strategy.de_lookback_days)
                if de is not None:
                    params = get_regime_params(de, cfg)
                    position = Position(
                        direction=signal,
                        entry_price=price,
                        entry_bar=i,
                        entry_time=dt.isoformat(),
                        tp_pct=params['tp_pct'],
                        sl_pct=params['sl_pct'],
                        regime=params['regime'],
                        de=de,
                    )
    
    # 统计
    return summarize(trades, dynamic_regime, verbose)


def summarize(trades: list, dynamic_regime: bool, verbose: bool = False) -> Dict:
    """统计并打印结果"""
    label = "动态 Regime" if dynamic_regime else "固定 Regime"
    
    if not trades:
        print(f"\n{label}: 0 笔交易")
        return {"trades": trades, "label": label}
    
    total_pnl = sum(t['pnl_pct'] for t in trades)
    avg_pnl = total_pnl / len(trades)
    wins = [t for t in trades if t['pnl_pct'] > 0]
    losses = [t for t in trades if t['pnl_pct'] <= 0]
    win_rate = len(wins) / len(trades) * 100
    
    tp_exits = [t for t in trades if 'TP' in t.get('reason', '')]
    sl_exits = [t for t in trades if 'SL' in t.get('reason', '')]
    timeout_exits = [t for t in trades if t.get('reason') == 'TIMEOUT']
    
    avg_bars = sum(t['bars_held'] for t in trades) / len(trades)
    
    print(f"\n{'─'*50}")
    print(f"📋 {label}")
    print(f"{'─'*50}")
    print(f"总交易: {len(trades)} 笔 | 胜率: {win_rate:.1f}% ({len(wins)}W/{len(losses)}L)")
    print(f"总收益: {total_pnl:+.2f}% | 平均: {avg_pnl:+.3f}%/笔")
    print(f"持仓: {avg_bars:.0f} bars ({avg_bars/2:.0f}h avg)")
    print(f"退出: TP={len(tp_exits)} SL={len(sl_exits)} TIMEOUT={len(timeout_exits)}")
    
    if wins:
        print(f"平均盈利: {sum(t['pnl_pct'] for t in wins)/len(wins):+.3f}%")
    if losses:
        print(f"平均亏损: {sum(t['pnl_pct'] for t in losses)/len(losses):+.3f}%")
    
    # Regime 分布
    regimes = {}
    for t in trades:
        r = t.get('entry_regime', 'unknown')
        regimes[r] = regimes.get(r, 0) + 1
    print(f"Regime: {regimes}")
    
    if verbose:
        print(f"\n  详细:")
        for t in trades:
            regime_change = f" → {t['exit_regime']}" if t['exit_regime'] != t['entry_regime'] else ""
            print(f"  {t['entry_time'][:16]} {t['direction']} "
                  f"${t['entry_price']:,.0f}→${t['exit_price']:,.0f} "
                  f"{t['pnl_pct']:+.2f}% [{t['reason']}] "
                  f"regime={t['entry_regime']}{regime_change} "
                  f"DE={t['de']:.3f} {t['bars_held']}bars")
    
    return {
        "trades": trades,
        "label": label,
        "total_pnl": total_pnl,
        "avg_pnl": avg_pnl,
        "win_rate": win_rate,
        "count": len(trades),
    }


def compare(coin='BTC', days=90, verbose=False):
    """对比固定 vs 动态 regime"""
    print(f"{'='*60}")
    print(f"📊 回测对比: {coin} {days}天")
    print(f"{'='*60}")
    
    fixed = run_backtest(coin, days, dynamic_regime=False, verbose=verbose)
    dynamic = run_backtest(coin, days, dynamic_regime=True, verbose=verbose)
    
    if fixed.get('total_pnl') is not None and dynamic.get('total_pnl') is not None:
        diff = dynamic['total_pnl'] - fixed['total_pnl']
        print(f"\n{'='*60}")
        print(f"📈 动态 vs 固定: {diff:+.2f}% {'✅ 动态更好' if diff > 0 else '❌ 固定更好'}")
        print(f"   固定: {fixed['total_pnl']:+.2f}% ({fixed['count']}笔)")
        print(f"   动态: {dynamic['total_pnl']:+.2f}% ({dynamic['count']}笔)")
        print(f"{'='*60}")


if __name__ == '__main__':
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 90
    verbose = '--verbose' in sys.argv or '-v' in sys.argv
    compare('BTC', days, verbose)
