#!/usr/bin/env python3
"""
回测引擎 — 复用实盘代码
========================
核心原则：回测和实盘使用**完全相同**的判断逻辑。

架构：
- strategy.py 的信号判断逻辑 → 回测直接调用
- regime.py 的 DE/regime 判断 → 回测直接调用
- 本模块只负责：数据获取 + 模拟持仓管理 + 结果统计

与实盘一致性检查清单：
  [✓] 信号生成：strategy.detect_signal()
  [✓] Regime TP/SL：regime.get_regime_params()
  [✓] 动态 regime 收紧：strategy.should_tighten_tp()
  [✓] 入场价：下一根 bar 的 open（不是当前 close）
  [✓] SL/TP 判断：用 high/low（不是 close）— 实盘是链上 trigger order
  [✓] Early validation：2 bar 后检查 MFE < 0.8% 提前出局
  [✓] 交易费用：8.64 bps round-trip
  [✓] 单仓制：持仓期间不开新仓
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
from luckytrader.strategy import detect_signal, should_tighten_tp
from luckytrader.config import get_config

# 交易成本
FEE_ROUND_TRIP_PCT = 8.64 / 10000  # 0.000864


def get_historical_candles(coin: str, interval: str, days: int) -> list:
    """获取历史 K 线"""
    info = Info(skip_ws=True)
    end = int(time.time() * 1000)
    start = end - days * 24 * 3600 * 1000
    return info.candles_snapshot(coin, interval, start, end)


def compute_de_for_date(candles_1d: list, bar_time_ms: int, lookback_days: int = 7) -> Optional[float]:
    """计算某个时间点的 DE"""
    idx = len(candles_1d) - 1
    while idx >= 0 and int(candles_1d[idx]['t']) > bar_time_ms:
        idx -= 1
    if idx < lookback_days:
        return None
    window = candles_1d[idx - lookback_days:idx + 1]
    return compute_de(window, lookback_days)


class Position:
    """模拟持仓"""
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
        self.early_validated = False
        self.high_water = entry_price  # for MFE tracking

    def check_sl_tp(self, high: float, low: float, close: float,
                    bars_held: int, max_hold_bars: int) -> Optional[Tuple[str, float]]:
        """检查 SL/TP 是否触发 — 用 high/low（和实盘链上 trigger order 一致）

        顺序：SL 先检查（保守假设，和 BB backtest_v3 一致）
        """
        if self.direction == 'LONG':
            sl_price = self.entry_price * (1 - self.sl_pct)
            tp_price = self.entry_price * (1 + self.tp_pct)
            # SL: low 触及止损价
            if low <= sl_price:
                pnl = -self.sl_pct - FEE_ROUND_TRIP_PCT
                return ('SL', pnl)
            # TP: high 触及止盈价
            if high >= tp_price:
                pnl = self.tp_pct - FEE_ROUND_TRIP_PCT
                return ('TP', pnl)
        else:  # SHORT
            sl_price = self.entry_price * (1 + self.sl_pct)
            tp_price = self.entry_price * (1 - self.tp_pct)
            if high >= sl_price:
                pnl = -self.sl_pct - FEE_ROUND_TRIP_PCT
                return ('SL', pnl)
            if low <= tp_price:
                pnl = self.tp_pct - FEE_ROUND_TRIP_PCT
                return ('TP', pnl)

        # Timeout
        if bars_held >= max_hold_bars:
            if self.direction == 'LONG':
                pnl = (close - self.entry_price) / self.entry_price - FEE_ROUND_TRIP_PCT
            else:
                pnl = (self.entry_price - close) / self.entry_price - FEE_ROUND_TRIP_PCT
            return ('TIMEOUT', pnl)

        return None

    def check_early_validation(self, highs_since_entry: list, lows_since_entry: list,
                               ev_bars: int, ev_mfe_thr: float) -> Optional[float]:
        """Early validation: 开仓后 ev_bars 根 bar，检查 MFE 是否达到阈值

        Returns:
            MFE 值（如果应该出局），None 表示不需要出局或还没到检查时间
        """
        if self.early_validated:
            return None
        if len(highs_since_entry) < ev_bars:
            return None

        # 到了检查时间
        self.early_validated = True

        if self.direction == 'LONG':
            mfe = (max(highs_since_entry[:ev_bars]) - self.entry_price) / self.entry_price * 100
        else:
            mfe = (self.entry_price - min(lows_since_entry[:ev_bars])) / self.entry_price * 100

        if mfe < ev_mfe_thr:
            return mfe  # 应该出局
        return None  # 通过

    def update_regime(self, new_de: Optional[float], cfg) -> bool:
        """动态 regime 重估 — 调用 strategy.should_tighten_tp()"""
        new_tp = should_tighten_tp(self.tp_pct, new_de, cfg)
        if new_tp is None:
            return False
        self.tp_pct = new_tp
        self.regime = get_regime_params(new_de, cfg)['regime']
        return True


def run_backtest(coin: str = 'BTC', days: int = 90, dynamic_regime: bool = True,
                 early_validation: bool = True, verbose: bool = False) -> Dict:
    """
    统一回测引擎 — 与实盘完全一致

    Args:
        coin: 交易对
        days: 回测天数
        dynamic_regime: 是否启用动态 regime TP 调整
        early_validation: 是否启用 early validation（2 bar MFE 检查）
        verbose: 是否打印每笔交易详情

    Returns:
        dict with trades list and summary stats
    """
    cfg = get_config()
    max_hold_bars = int(cfg.risk.max_hold_hours * 2)  # 30m bars
    ev_bars = cfg.strategy.early_validation_bars
    ev_mfe_thr = cfg.strategy.early_validation_mfe

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

    for i in range(start_idx, len(candles_30m) - 1):  # -1: need next bar for entry
        bar_time = int(candles_30m[i]['t'])
        dt = datetime.fromtimestamp(bar_time / 1000, tz=timezone.utc)
        high = float(candles_30m[i]['h'])
        low = float(candles_30m[i]['l'])
        close = float(candles_30m[i]['c'])

        # ── 持仓管理 ──
        if position:
            bars_held = i - position.entry_bar

            # Early validation: 2 bar 后检查 MFE
            if early_validation and not position.early_validated and bars_held >= 1:
                highs_since = [float(candles_30m[j]['h'])
                               for j in range(position.entry_bar + 1, i + 1)]
                lows_since = [float(candles_30m[j]['l'])
                              for j in range(position.entry_bar + 1, i + 1)]
                mfe = position.check_early_validation(highs_since, lows_since,
                                                       ev_bars, ev_mfe_thr)
                if mfe is not None:
                    # 假突破，提前出局
                    if position.direction == 'LONG':
                        pnl = (close - position.entry_price) / position.entry_price - FEE_ROUND_TRIP_PCT
                    else:
                        pnl = (position.entry_price - close) / position.entry_price - FEE_ROUND_TRIP_PCT
                    trades.append({
                        'entry_time': position.entry_time,
                        'exit_time': dt.isoformat(),
                        'direction': position.direction,
                        'entry_price': position.entry_price,
                        'exit_price': close,
                        'pnl_pct': pnl * 100,
                        'reason': f'EARLY_EXIT(MFE={mfe:.3f}%)',
                        'bars_held': bars_held,
                        'entry_regime': position.entry_regime,
                        'exit_regime': position.regime,
                        'de': position.de,
                    })
                    position = None
                    continue

            # 动态 regime 重估（和实盘一样，每天检查）
            if dynamic_regime and bars_held > 0 and bars_held % 48 == 0:
                de = compute_de_for_date(candles_1d, bar_time, cfg.strategy.de_lookback_days)
                position.update_regime(de, cfg)

            # 检查 SL/TP — 用 high/low（和实盘链上 trigger order 一致）
            exit_result = position.check_sl_tp(high, low, close, bars_held, max_hold_bars)
            if exit_result:
                reason, pnl = exit_result
                # 确定退出价
                if 'SL' in reason:
                    if position.direction == 'LONG':
                        exit_price = position.entry_price * (1 - position.sl_pct)
                    else:
                        exit_price = position.entry_price * (1 + position.sl_pct)
                elif 'TP' in reason:
                    if position.direction == 'LONG':
                        exit_price = position.entry_price * (1 + position.tp_pct)
                    else:
                        exit_price = position.entry_price * (1 - position.tp_pct)
                else:
                    exit_price = close

                trades.append({
                    'entry_time': position.entry_time,
                    'exit_time': dt.isoformat(),
                    'direction': position.direction,
                    'entry_price': position.entry_price,
                    'exit_price': exit_price,
                    'pnl_pct': pnl * 100,
                    'reason': f'{reason}({position.regime}:{position.tp_pct*100:.0f}%)',
                    'bars_held': bars_held,
                    'entry_regime': position.entry_regime,
                    'exit_regime': position.regime,
                    'de': position.de,
                })
                position = None

        # ── 信号检测（无持仓时）──
        if position is None:
            signal = detect_signal(candles_30m, candles_4h, i, cfg)
            if signal:
                de = compute_de_for_date(candles_1d, bar_time, cfg.strategy.de_lookback_days)
                if de is not None:
                    params = get_regime_params(de, cfg)
                    # 入场价 = 下一根 bar 的 open（不是当前 close）
                    entry_price = float(candles_30m[i + 1]['o'])
                    position = Position(
                        direction=signal,
                        entry_price=entry_price,
                        entry_bar=i + 1,  # 实际入场在下一根 bar
                        entry_time=dt.isoformat(),
                        tp_pct=params['tp_pct'],
                        sl_pct=params['sl_pct'],
                        regime=params['regime'],
                        de=de,
                    )

    # 统计
    return summarize(trades, dynamic_regime, early_validation, verbose)


def summarize(trades: list, dynamic_regime: bool, early_validation: bool = True,
              verbose: bool = False) -> Dict:
    """统计并打印结果"""
    features = []
    if dynamic_regime:
        features.append("动态Regime")
    if early_validation:
        features.append("EarlyVal")
    label = " + ".join(features) if features else "基础"

    if not trades:
        print(f"\n{label}: 0 笔交易")
        return {"trades": trades, "label": label}

    total_pnl = sum(t['pnl_pct'] for t in trades)
    avg_pnl = total_pnl / len(trades)
    wins = [t for t in trades if t['pnl_pct'] > 0]
    losses = [t for t in trades if t['pnl_pct'] <= 0]
    win_rate = len(wins) / len(trades) * 100

    # Exit reasons
    early_exits = [t for t in trades if 'EARLY_EXIT' in t.get('reason', '')]
    tp_exits = [t for t in trades if 'TP' in t.get('reason', '') and 'EARLY' not in t.get('reason', '')]
    sl_exits = [t for t in trades if 'SL' in t.get('reason', '')]
    timeout_exits = [t for t in trades if 'TIMEOUT' in t.get('reason', '')]

    avg_bars = sum(t['bars_held'] for t in trades) / len(trades)

    print(f"\n{'─'*55}")
    print(f"📋 {label} | Fee: {FEE_ROUND_TRIP_PCT*10000:.1f}bps RT")
    print(f"{'─'*55}")
    print(f"总交易: {len(trades)} 笔 | 胜率: {win_rate:.1f}% ({len(wins)}W/{len(losses)}L)")
    print(f"总收益: {total_pnl:+.2f}% | 平均: {avg_pnl:+.3f}%/笔")
    print(f"持仓: {avg_bars:.0f} bars ({avg_bars/2:.0f}h avg)")
    print(f"退出: TP={len(tp_exits)} SL={len(sl_exits)} TO={len(timeout_exits)} EARLY={len(early_exits)}")

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
