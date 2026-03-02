#!/usr/bin/env python3
"""
Lucky Trading WebSocket Monitor v1.1
实时监控 Hyperliquid WebSocket，自动执行交易信号

架构：
- WebSocketManager: WebSocket连接管理
- SignalProcessor: 实时信号检测和去重
- TradeExecutor: 自动交易执行
- NotificationManager: Discord通知
- StateManager: 状态持久化
- WSMonitor: 主控制器
"""

import asyncio
import json
import time
import logging
import signal as sig
import sys
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict
from dataclasses import dataclass

# WebSocket
import websockets
from websockets.exceptions import ConnectionClosedError, WebSocketException

# 现有模块集成
from luckytrader.config import get_config, get_workspace_dir, TRADING_COINS
from luckytrader.signal import analyze, format_report, get_recent_fills
from luckytrader import execute
from luckytrader import trailing
from luckytrader.trade import get_market_price

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# WebSocket配置
WS_URL = "wss://api.hyperliquid.xyz/ws"
HEARTBEAT_TIMEOUT = 60  # 60秒心跳超时
MAX_RECONNECT_DELAY = 120  # 最大重连延迟
MAX_RETRIES = 10  # 最大重试次数


@dataclass
class KlineData:
    """K线数据结构"""
    coin: str
    interval: str
    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float


def validate_price_change(old_price: float, new_price: float, threshold: float = 0.15) -> bool:
    """校验价格变动合理性"""
    if new_price <= 0:
        return False

    change_pct = abs(new_price - old_price) / old_price
    return change_pct <= threshold


def normalize_ws_kline(raw: Dict) -> Dict:
    """将 Hyperliquid WS K线格式转为内部标准格式

    WS 格式: {t, T, s, i, o, c, h, l, v, n}
    内部格式: {coin, interval, time, open, close, high, low, volume}
    """
    return {
        "coin": raw["s"],
        "interval": raw["i"],
        "time": raw["t"],
        "open": raw["o"],
        "close": raw["c"],
        "high": raw["h"],
        "low": raw["l"],
        "volume": raw["v"],
    }


def parse_websocket_message(message: str) -> Optional[Dict]:
    """解析WebSocket消息"""
    try:
        return json.loads(message)
    except json.JSONDecodeError:
        logger.error(f"Invalid JSON message: {message[:100]}")
        return None


class WebSocketManager:
    """WebSocket连接管理器"""

    def __init__(self):
        self.websocket = None
        self.connected = False
        self.reconnect_count = 0
        self.last_message_time = 0
        self.heartbeat_task = None
        self._reconnect_lock = asyncio.Lock()

    async def connect(self) -> bool:
        """建立WebSocket连接"""
        try:
            logger.info(f"Connecting to {WS_URL}")
            self.websocket = await websockets.connect(WS_URL)
            self.connected = True
            self.last_message_time = time.time()
            logger.info("WebSocket connected successfully")
            return True

        except Exception as e:
            logger.error(f"WebSocket connection failed: {e}")
            self.connected = False
            return False

    async def disconnect(self):
        """断开连接——安全处理已关闭的 socket"""
        if self.websocket:
            try:
                await self.websocket.close()
            except Exception as e:
                logger.debug(f"WebSocket close error (already disconnected): {e}")
            finally:
                self.websocket = None
                self.connected = False
            logger.info("WebSocket disconnected")

    async def connect_with_retry(self) -> bool:
        """带重试的连接建立"""
        for attempt in range(MAX_RETRIES):
            if await self.connect():
                self.reconnect_count = attempt
                return True

            if attempt < MAX_RETRIES - 1:
                delay = min(2 ** attempt, MAX_RECONNECT_DELAY)
                logger.info(f"Retrying connection in {delay}s (attempt {attempt + 1}/{MAX_RETRIES})")
                await asyncio.sleep(delay)

        logger.error(f"Failed to connect after {MAX_RETRIES} attempts")
        return False

    async def subscribe_klines(self, coin: str, interval: str = "30m"):
        """订阅K线数据"""
        if not self.connected or not self.websocket:
            raise Exception("WebSocket not connected")

        subscription = {
            "method": "subscribe",
            "subscription": {
                "type": "candle",
                "coin": coin,
                "interval": interval
            }
        }

        await self.websocket.send(json.dumps(subscription))
        logger.info(f"Subscribed to {coin} {interval} candles")

    async def receive_message(self) -> Optional[Dict]:
        """接收WebSocket消息"""
        if not self.connected or not self.websocket:
            return None

        try:
            message = await asyncio.wait_for(
                self.websocket.recv(),
                timeout=HEARTBEAT_TIMEOUT
            )

            self.last_message_time = time.time()
            return parse_websocket_message(message)

        except asyncio.TimeoutError:
            logger.warning("WebSocket timeout - no message received")
            return None
        except ConnectionClosedError:
            logger.warning("WebSocket connection closed")
            self.connected = False
            return None
        except Exception as e:
            logger.error(f"Error receiving message: {e}")
            self.connected = False  # 任何异常都标记为断线，防止无限循环
            return None

    async def subscribe_all_coins(self, interval: str = "30m"):
        """Subscribe to klines for all trading coins."""
        for coin in TRADING_COINS:
            await self.subscribe_klines(coin, interval)

    async def heartbeat_monitor(self):
        """心跳监控——持续运行，支持 CancelledError 优雅退出"""
        try:
            while True:
                if not self.connected:
                    await asyncio.sleep(5)
                    continue

                await asyncio.sleep(HEARTBEAT_TIMEOUT)

                if self.connected and time.time() - self.last_message_time > HEARTBEAT_TIMEOUT:
                    logger.warning("Heartbeat timeout - triggering reconnect")
                    if await self.reconnect_with_lock():
                        await self.subscribe_all_coins()
        except asyncio.CancelledError:
            logger.info("Heartbeat monitor cancelled")
            raise  # 重新抛出让 gather 正确处理

    async def reconnect(self):
        """重连逻辑——只负责连接，不负责订阅（由调用者处理）"""
        logger.info("Reconnecting WebSocket...")
        await self.disconnect()
        return await self.connect_with_retry()

    async def reconnect_with_lock(self):
        """带锁的重连——防止并发重连"""
        async with self._reconnect_lock:
            if not self.connected:
                return await self.reconnect()
            return self.connected


class SignalProcessor:
    """信号处理器 — per-coin instance"""

    def __init__(self, coin: str = "BTC", cache_size: int = 30):
        self.coin = coin
        self.kline_cache = deque(maxlen=cache_size)
        self.signal_history = []  # [(timestamp, signal), ...]
        self.duplicate_window = 600  # 10分钟去重窗口
        self.last_price = None
        self._current_candle_time = 0  # 当前 K 线时间戳
        self._candle_closed = False  # 是否检测到新 K 线（即上一根收盘）

    def add_kline(self, kline_data: Dict):
        """添加K线数据到缓存"""
        # 数据校验
        if not self.validate_kline(kline_data):
            return False

        # 转换为标准格式
        kline = KlineData(
            coin=kline_data["coin"],
            interval=kline_data.get("interval", "30m"),
            time=int(kline_data["time"]),
            open=float(kline_data["open"]),
            high=float(kline_data["high"]),
            low=float(kline_data["low"]),
            close=float(kline_data["close"]),
            volume=float(kline_data["volume"])
        )

        self.kline_cache.append(kline)
        self.last_price = kline.close

        # 检测 K 线收盘：时间戳变化 = 新 K 线开始 = 上一根收盘
        if self._current_candle_time and kline.time != self._current_candle_time:
            self._candle_closed = True
            logger.info(f"Candle closed, new candle at {kline.time}")
        self._current_candle_time = kline.time

        logger.debug(f"Added kline: {kline.coin} ${kline.close:,.2f} vol={kline.volume:.3f}")
        return True

    def validate_kline(self, kline_data: Dict) -> bool:
        """校验K线数据"""
        try:
            close_price = float(kline_data["close"])
            volume = float(kline_data["volume"])

            # 价格必须为正
            if close_price <= 0:
                logger.warning(f"Invalid price: {close_price}")
                return False

            # 成交量异常检查（允许为0）
            if volume < 0:
                logger.warning(f"Invalid volume: {volume}")
                return False

            # 价格突变检查
            if self.last_price and not validate_price_change(self.last_price, close_price):
                logger.warning(f"Price spike detected: {self.last_price} -> {close_price}")
                return False

            return True

        except (ValueError, KeyError) as e:
            logger.error(f"Kline data validation error: {e}")
            return False

    def process_signal(self) -> Optional[Dict]:
        """处理信号检测

        只在 30m K 线收盘时调用 analyze()（时间戳变化 = 新 K 线开始 = 上一根收盘）。
        与回测一致：信号基于已收盘 K 线，入场用 next-open。
        """
        if not self._candle_closed:
            return None
        self._candle_closed = False

        try:
            # 调用现有signal.analyze()函数
            result = analyze(self.coin)

            if "error" in result:
                logger.error(f"Signal analysis error: {result['error']}")
                return None

            signal = result.get("signal", "HOLD")

            # 检查是否应该执行
            if signal != "HOLD" and self.should_execute_signal(signal):
                logger.info(f"Signal detected: {signal} - {'; '.join(result.get('signal_reasons', []))}")
                return result

            return None

        except Exception as e:
            logger.error(f"Signal processing error: {e}")
            return None

    def should_execute_signal(self, signal: str) -> bool:
        """检查信号是否应该执行（去重）"""
        now = time.time()

        # 清理过期历史
        self.signal_history = [
            (ts, s) for ts, s in self.signal_history
            if now - ts < self.duplicate_window
        ]

        # 检查重复
        for ts, s in self.signal_history:
            if s == signal:
                logger.debug(f"Duplicate signal filtered: {signal}")
                return False

        # 记录新信号
        self.signal_history.append((now, signal))
        return True


class TradeExecutor:
    """交易执行器

    关键设计：直接调用 execute.open_position()，而非 execute.execute()。
    因为 execute() 会重新调 analyze() 分析信号，可能推翻 WS 已检测到的信号。
    WS 检测 → 直接执行，不做二次确认。

    SL/TP 触发检测：定期检查 position_state.json vs 链上持仓，
    发现不一致时执行清理（记录交易、更新状态、通知）。
    """

    def __init__(self):
        self.trailing_task = None
        self.position_check_interval = 60  # 1分钟检查一次移动止损
        self._last_position_check = 0
        self._position_check_cooldown = 30  # 每 30 秒检查一次持仓状态
        self._last_regime_check = 0
        self._regime_check_interval = 3600  # DE 基于日线，每小时重算一次足够
        self._early_validation_done = {}  # per-coin: {coin: True/False}
        self._opening_lock = False  # 防止竞态条件导致重复开仓

    async def execute_signal(self, signal_result: Dict, coin: str = "BTC") -> Dict:
        """执行交易信号——直接开仓，不重新分析（async，不阻塞事件循环）"""
        try:
            # 防竞态：如果正在开仓中，直接跳过
            if self._opening_lock:
                logger.info("Opening lock active, skipping duplicate signal")
                return {"action": "SKIP", "reason": "opening_lock"}

            # 检查该币种是否有持仓（async，避免阻塞事件循环）
            if await asyncio.to_thread(self.has_position, coin):
                logger.info(f"Already has {coin} position, skipping signal")
                return {"action": "SKIP", "reason": "has_position"}

            signal = signal_result.get("signal")
            if signal == "HOLD":
                return {"action": "HOLD"}

            # 加锁，防止并发开仓
            self._opening_lock = True
            logger.info(f"Executing {signal} {coin} signal (direct open, no re-analysis)...")

            try:
                result = await asyncio.to_thread(execute.open_position, signal, signal_result, coin)
            finally:
                self._opening_lock = False

            if result.get("action") == "OPENED":
                self._early_validation_done.pop(coin, None)  # 重置该币种的 early validation
                asyncio.create_task(self.start_trailing_monitor())

            return result

        except Exception as e:
            self._opening_lock = False
            logger.error(f"Trade execution error: {e}")
            return {"action": "ERROR", "error": str(e)}

    def has_position(self, coin: str = "BTC") -> bool:
        """检查指定币种是否有持仓"""
        try:
            position = execute.get_position(coin)
            return position is not None
        except Exception as e:
            logger.error(f"Position check error: {e}")
            return False

    async def check_position_closed_by_trigger(self, coin: str = "BTC") -> Optional[Dict]:
        """检查 SL/TP 是否被 Hyperliquid 自动触发（async，不阻塞事件循环）

        对比 position_state.json（本地记录）vs 链上实际持仓。
        如果本地有记录但链上无持仓，说明 SL/TP 被触发了。

        返回平仓信息（含盈亏），或 None（无变化）。
        """
        now = time.time()
        if now - self._last_position_check < self._position_check_cooldown:
            return None
        self._last_position_check = now

        try:
            # 在线程中执行同步 REST API 调用
            state = await asyncio.to_thread(execute.load_state, coin)
            if not state.get("position"):
                return None  # 本地也没持仓记录，正常

            # 本地有持仓记录，检查链上
            position = await asyncio.to_thread(execute.get_position, coin)
            if position is not None:
                return None  # 链上仍有持仓，正常

            # 本地有记录但链上无持仓 → SL/TP 被触发！
            sp = state["position"]
            logger.warning(f"Position closed by trigger: {sp['direction']} {coin}")

            entry = sp["entry_price"]
            expected_close_side = "SELL" if sp["direction"] == "LONG" else "BUY"
            fills = await asyncio.to_thread(get_recent_fills, 5)
            close_fill = next(
                (f for f in fills if f.get("coin") == coin and f.get("side") == expected_close_side),
                None
            )
            if close_fill:
                close_price = float(close_fill["price"])
            else:
                close_price = await asyncio.to_thread(get_market_price, coin)

            if sp["direction"] == "LONG":
                pnl_pct = (close_price - entry) / entry * 100
            else:
                pnl_pct = (entry - close_price) / entry * 100

            sl = sp.get("sl_price", 0)
            tp = sp.get("tp_price", 0)
            # Classify exit reason: first try proximity check with 1% tolerance
            if sp["direction"] == "LONG":
                reason = "TP" if close_price >= tp * 0.99 else "SL" if close_price <= sl * 1.01 else None
            else:
                reason = "TP" if close_price <= tp * 1.01 else "SL" if close_price >= sl * 0.99 else None

            if reason is None:
                # Fallback: compare distance to SL vs TP; closer one wins
                # Also handles trailing stop (SL moved to breakeven+)
                dist_sl = abs(close_price - sl) if sl else float('inf')
                dist_tp = abs(close_price - tp) if tp else float('inf')
                if dist_sl < dist_tp:
                    reason = "SL"
                elif dist_tp < dist_sl:
                    reason = "TP"
                else:
                    # PnL-based: positive = TP, negative/zero = SL
                    reason = "TP" if pnl_pct > 0 else "SL"
                logger.info(f"Exit classified by distance fallback: {reason} "
                            f"(close={close_price}, sl={sl}, tp={tp}, dist_sl={dist_sl:.2f}, dist_tp={dist_tp:.2f})")

            await asyncio.to_thread(execute.record_trade_result, pnl_pct, sp["direction"], coin, reason)
            await asyncio.to_thread(execute.log_trade, "CLOSED_BY_TRIGGER", coin, sp["direction"], sp["size"],
                             close_price, None, None, f"{reason} 触发, PnL {pnl_pct:+.2f}%")
            await asyncio.to_thread(execute.save_state, {"position": None}, coin)

            # Timestamps for notification
            entry_time = sp.get("entry_time", "")
            close_time = ""
            if close_fill and close_fill.get("time"):
                # Hyperliquid fills use epoch ms
                try:
                    close_ts = int(close_fill["time"]) / 1000
                    from datetime import timezone as _tz
                    close_time = datetime.fromtimestamp(close_ts, tz=_tz.utc).strftime("%Y-%m-%d %H:%M UTC")
                except (ValueError, TypeError) as e:
                    logger.debug(f"Failed to parse fill timestamp: {e}")

            return {
                "action": "CLOSED_BY_TRIGGER",
                "reason": reason,
                "pnl_pct": pnl_pct,
                "direction": sp["direction"],
                "coin": coin,
                "entry_price": entry,
                "close_price": close_price,
                "entry_time": entry_time,
                "close_time": close_time,
            }

        except Exception as e:
            logger.error(f"Position trigger check error: {e}")
            return None

    def can_open_position(self, direction: str) -> bool:
        """检查是否可以开仓"""
        return not self.has_position()

    async def start_trailing_monitor(self):
        """启动移动止损监控"""
        if self.trailing_task and not self.trailing_task.done():
            return  # 已经在运行

        self.trailing_task = asyncio.create_task(self._trailing_loop())
        logger.info("Started trailing stop monitor")

    def stop_trailing_monitor(self):
        """停止移动止损监控"""
        if self.trailing_task and not self.trailing_task.done():
            self.trailing_task.cancel()
            logger.info("Stopped trailing stop monitor")

    async def _trailing_loop(self):
        """移动止损循环"""
        # 启动时先检查孤儿仓位（30 秒超时，不阻塞主循环）
        try:
            reconciled = await asyncio.wait_for(
                asyncio.to_thread(execute.reconcile_orphan_positions),
                timeout=30
            )
            if reconciled:
                logger.warning(f"Reconciled {len(reconciled)} orphan positions: {reconciled}")
        except asyncio.TimeoutError:
            logger.error("Orphan position check timed out (30s), skipping")
        except Exception as e:
            logger.error(f"Orphan position check failed: {e}")
        
        logger.info("Trailing loop: entering main loop")
        while True:
            try:
                logger.debug("Trailing loop: checking position...")
                has_pos = await asyncio.wait_for(
                    asyncio.to_thread(self.has_position),
                    timeout=15
                )
                if not has_pos:
                    logger.info("No position found, stopping trailing monitor")
                    break

                # ─── 1小时方向确认（早期验证）───
                # ─── Per-coin early validation ───
                try:
                    for ev_coin in execute.TRADING_COINS:
                        if self._early_validation_done.get(ev_coin):
                            continue
                        coin_state = await asyncio.to_thread(execute.load_state, ev_coin)
                        pos = coin_state.get("position") if coin_state else None
                        if not (pos and pos.get("entry_time")):
                            continue
                        from datetime import datetime, timezone
                        entry_time = datetime.fromisoformat(pos["entry_time"])
                        elapsed_min = (datetime.now(timezone.utc) - entry_time).total_seconds() / 60
                        ev_bars = execute._cfg.strategy.early_validation_bars
                        ev_minutes = ev_bars * 30  # 每根30m = 30分钟
                        ev_mfe_thr = execute._cfg.strategy.early_validation_mfe

                        if elapsed_min < ev_minutes:
                            continue

                        entry_price = pos["entry_price"]
                        direction = pos["direction"]

                        # 获取开仓后的K线数据计算 MFE
                        from hyperliquid.info import Info as _Info
                        _info = _Info(skip_ws=True)
                        _end = int(time.time() * 1000)
                        _start = int(entry_time.timestamp() * 1000)
                        candles = _info.candles_snapshot(ev_coin, '30m', _start, _end)

                        if not candles or len(candles) < 2:
                            # K线数据不足，下个循环重试（不标记 done）
                            logger.warning(f"Early validation {ev_coin}: insufficient candles ({len(candles) if candles else 0}), will retry next loop")
                            continue

                        # K线数据够了，执行检查，标记该币种 done
                        highs = [float(c['h']) for c in candles[1:]]  # 跳过入场那根
                        lows = [float(c['l']) for c in candles[1:]]
                        if direction == 'LONG':
                            mfe = (max(highs) - entry_price) / entry_price * 100
                        else:
                            mfe = (entry_price - min(lows)) / entry_price * 100

                        logger.info(f"Early validation {ev_coin}: {direction} @ ${entry_price:,.0f}, "
                                  f"elapsed {elapsed_min:.0f}min, MFE={mfe:.3f}%, threshold={ev_mfe_thr}%")

                        if mfe < ev_mfe_thr:
                            # 假突破，提前出局
                            logger.warning(f"❌ Early validation FAILED {ev_coin}: MFE {mfe:.3f}% < {ev_mfe_thr}%, closing position")
                            print(f"❌ {ev_coin} 1h方向确认失败: MFE {mfe:.3f}% < {ev_mfe_thr}%, 提前出局")

                            size = abs(pos["size"])
                            is_long = direction == "LONG"
                            pnl_pct = execute.compute_pnl_pct(direction, entry_price, execute.get_market_price(ev_coin))

                            execute.close_and_cleanup(
                                ev_coin, is_long, size, reason="EARLY_EXIT",
                                pnl_pct=pnl_pct,
                                extra_msg=f"1h方向确认失败 MFE={mfe:.3f}%<{ev_mfe_thr}%"
                            )
                            # Mark done ONLY after successful close
                            self._early_validation_done[ev_coin] = True
                        else:
                            logger.info(f"✅ Early validation PASSED {ev_coin}: MFE {mfe:.3f}% >= {ev_mfe_thr}%")
                            print(f"✅ {ev_coin} 1h方向确认通过: MFE {mfe:.3f}% >= {ev_mfe_thr}%")
                            self._early_validation_done[ev_coin] = True
                except Exception as e:
                    # 异常不标记 done，下个循环重试
                    logger.error(f"Early validation error (will retry): {e}")

                # 在线程中调用同步 trailing 模块，避免阻塞事件循环
                alerts = await asyncio.to_thread(trailing.main)

                if alerts:
                    for alert in alerts:
                        logger.warning(f"Trailing stop alert: {alert}")

                # 动态 regime 重估：如果市场从趋势变横盘，收紧 TP（每小时一次）
                now_ts = time.time()
                if now_ts - self._last_regime_check >= self._regime_check_interval:
                    self._last_regime_check = now_ts
                    for rr_coin in execute.TRADING_COINS:
                        try:
                            coin_state = await asyncio.to_thread(execute.load_state, rr_coin)
                            pos = coin_state.get("position")
                            if not (pos and pos.get("regime_tp_pct", 0) > 0.02):
                                continue
                            # 只在 TP > 2%（即趋势市开仓）时检查
                            result = await asyncio.to_thread(execute.reeval_regime_tp, pos)
                            if result:
                                logger.warning(f"Regime re-eval {rr_coin}: {result['old_regime']}→{result['new_regime']}, "
                                             f"TP {result['old_tp_pct']*100:.0f}%→{result['new_tp_pct']*100:.0f}%")
                                print(f"🔄 {rr_coin} Regime 动态调整: DE={result['de']:.3f} "
                                      f"{result['old_regime']}→{result['new_regime']} "
                                      f"TP {result['old_tp_pct']*100:.0f}%→{result['new_tp_pct']*100:.0f}% "
                                      f"(${result['new_tp_price']:,.0f})")
                        except Exception as e:
                            logger.error(f"Regime re-eval error for {rr_coin}: {e}")

                await asyncio.sleep(self.position_check_interval)

            except asyncio.CancelledError:
                logger.info("Trailing stop task cancelled (shutdown)")
                break
            except Exception as e:
                logger.error(f"Trailing stop error: {e}")
                await asyncio.sleep(10)  # 短暂暂停后重试


class NotificationManager:
    """通知管理器"""

    def __init__(self):
        config = get_config()
        self.discord_channel_id = config.notifications.discord_channel_id
        self.discord_mentions = config.notifications.discord_mentions
        self._config = config
        self.notification_history = []  # [(timestamp, type, message), ...]
        self.notification_window = 60  # 1分钟去重窗口

    @staticmethod
    def _format_sgt(ts_str: Optional[str] = None) -> str:
        """Format a timestamp (ISO string or None=now) as SGT string."""
        from datetime import timezone as tz, timedelta as td
        sgt = tz(td(hours=8))
        if ts_str:
            try:
                dt = datetime.fromisoformat(str(ts_str))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=tz.utc)
                return dt.astimezone(sgt).strftime("%Y-%m-%d %H:%M SGT")
            except Exception:
                return str(ts_str)
        return datetime.now(tz.utc).astimezone(sgt).strftime("%Y-%m-%d %H:%M SGT")

    def notify_trade_opened(self, trade_info: Dict):
        """交易开仓通知"""
        message = f"🚀 **开仓** {trade_info['direction']} {trade_info.get('coin', 'BTC')}\n"
        message += f"💰 入场: ${trade_info['entry']:,.2f} | 数量: {trade_info['size']}\n"

        if 'sl' in trade_info and 'tp' in trade_info:
            sl_pct = self._config.risk.stop_loss_pct * 100
            tp_pct = self._config.risk.take_profit_pct * 100
            message += f"🛑 止损: ${trade_info['sl']:,.2f} (-{sl_pct:.0f}%) | "
            message += f"🎯 止盈: ${trade_info['tp']:,.2f} (+{tp_pct:.0f}%)\n"

        message += f"⏰ 最长持仓: {self._config.risk.max_hold_hours}h\n"
        message += f"🕐 开仓时间: {self._format_sgt()}"

        self._send_discord_message(message)

    def notify_trade_closed(self, close_info: Dict):
        """交易平仓通知"""
        pnl_pct = close_info.get('pnl_pct', 0)
        reason = close_info.get('reason', 'UNKNOWN')

        emoji = "🎯" if reason == "TP" else "🛑" if reason == "SL" else "⏰"

        message = f"{emoji} **平仓** {close_info.get('direction', '')} {close_info.get('coin', 'BTC')} — {reason}触发\n"
        message += f"💰 入场: ${close_info.get('entry_price', 0):,.2f} → 平仓: ~${close_info.get('close_price', 0):,.2f}\n"
        message += f"📊 盈亏: {pnl_pct:+.2f}%\n"

        entry_time = close_info.get('entry_time')
        close_time = close_info.get('close_time')
        entry_time_str = self._format_sgt(entry_time) if entry_time else "未知"
        exit_time_str = self._format_sgt(close_time) if close_time else self._format_sgt()
        message += f"🕐 入场: {entry_time_str} → 出场: {exit_time_str}"

        self._send_discord_message(message)

    def notify_signal_detected(self, signal_info: Dict):
        """信号检测通知"""
        signal = signal_info.get('signal', 'UNKNOWN')
        reasons = signal_info.get('signal_reasons', [])
        price = signal_info.get('price', 0)

        coin = signal_info.get('coin', '???')
        message = f"📡 **信号检测** {coin} {signal}\n"
        message += f"💰 价格: ${price:,.2f}\n"
        if reasons:
            message += f"📋 理由: {'; '.join(reasons)}"

        self._send_discord_message(message)

    def notify_error(self, error_message: str, critical: bool = False):
        """错误通知。critical=True 绕过去重（安全相关告警）"""
        if critical or self.should_send_notification("ERROR", error_message):
            message = f"⚠️ **系统错误**\n{error_message}"
            self._send_discord_message(message, force=critical)

    def notify_critical_error(self, error_message: str):
        """关键错误告警"""
        message = f"🚨🚨🚨 **关键错误** 🚨🚨🚨\n{error_message}\n需要立即人工检查！"
        self._send_discord_message(message, force=True)

    def should_send_notification(self, notification_type: str, message: str) -> bool:
        """检查是否应该发送通知（去重）"""
        now = time.time()

        # 清理过期通知
        self.notification_history = [
            (ts, ntype, msg) for ts, ntype, msg in self.notification_history
            if now - ts < self.notification_window
        ]

        # 检查重复
        for ts, ntype, msg in self.notification_history:
            if ntype == notification_type and msg == message:
                return False

        # 记录新通知
        self.notification_history.append((now, notification_type, message))
        return True

    # --- Async wrappers（从 async _message_loop 调用，避免 subprocess.run 阻塞事件循环）---

    async def async_notify_trade_closed(self, close_info: Dict):
        await asyncio.to_thread(self.notify_trade_closed, close_info)

    async def async_notify_signal_detected(self, signal_info: Dict):
        await asyncio.to_thread(self.notify_signal_detected, signal_info)

    async def async_notify_error(self, error_message: str, critical: bool = False):
        await asyncio.to_thread(self.notify_error, error_message, critical)

    def _send_discord_message(self, message: str, force: bool = False):
        """发送Discord消息"""
        try:
            import subprocess
            import shutil

            if not force and not self.should_send_notification("GENERAL", message):
                return

            full_message = f"{self.discord_mentions}\n{message}"

            # 使用openclaw发送消息
            openclaw_path = shutil.which("openclaw") or str(Path.home() / ".local/bin/openclaw")

            cmd = [
                openclaw_path, "message", "send",
                "--channel", "discord",
                "--target", self.discord_channel_id,
                "--message", full_message,
            ]

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                logger.info("Discord notification sent")
            else:
                logger.error(f"Discord send failed: {result.stderr}")

        except Exception as e:
            logger.error(f"Discord notification failed: {e}")


class StateManager:
    """状态管理器"""

    def __init__(self, state_file: Optional[Path] = None):
        workspace = get_workspace_dir()
        self.state_file = state_file or (workspace / "memory/trading/ws_monitor_state.json")
        self.state = self.load_state()

    def load_state(self) -> Dict:
        """加载状态"""
        if self.state_file.exists():
            try:
                with open(self.state_file) as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Failed to load state: {e}")

        return {
            "websocket": {"connected": False, "reconnect_count": 0},
            "trading": {"last_signal": None, "last_signal_time": 0},
            "monitoring": {"start_time": None, "processed_messages": 0}
        }

    def save_state(self, state: Dict):
        """保存状态（原子写入：temp file + rename）"""
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp_file = self.state_file.with_suffix('.tmp')
            with open(tmp_file, 'w') as f:
                json.dump(state, f, indent=2, default=str)
            tmp_file.rename(self.state_file)
            self.state = state
        except Exception as e:
            logger.error(f"Failed to save state: {e}")

    def update_websocket_status(self, connected: bool, reconnect_count: int = 0):
        """更新WebSocket状态"""
        self.state["websocket"] = {
            "connected": connected,
            "last_update": datetime.now(timezone.utc).isoformat(),
            "reconnect_count": reconnect_count
        }
        self.save_state(self.state)

    def update_trading_status(self, signal: str):
        """更新交易状态"""
        self.state["trading"] = {
            "last_signal": signal,
            "last_signal_time": time.time(),
            "last_update": datetime.now(timezone.utc).isoformat()
        }
        self.save_state(self.state)

    def recover_on_startup(self) -> Dict:
        """启动恢复"""
        recovery_info = {
            "has_position": False,
            "position": None,
            "signal_history": [],
            "last_run_time": self.state.get("monitoring", {}).get("start_time")
        }

        # 检查现有持仓（所有交易币种）— 链上为准，同步到本地 state
        try:
            from luckytrader.execute import get_position, load_state, save_state
            for coin in TRADING_COINS:
                position = get_position(coin)
                if position:
                    recovery_info["has_position"] = True
                    recovery_info["position"] = position
                    logger.info(f"Recovered position: {position['direction']} {position['size']} {coin}")

                    # 同步到本地 state（如果本地 state 丢失）
                    local = load_state(coin)
                    if not local.get("position"):
                        logger.warning(f"Local state missing for {coin}, syncing from chain")
                        save_state({
                            "position": {
                                "coin": coin,
                                "direction": position["direction"],
                                "size": abs(position["size"]),
                                "entry_price": position["entry_price"],
                                "entry_time": datetime.now(timezone.utc).isoformat(),
                            }
                        }, coin=coin)
        except Exception as e:
            logger.error(f"Position recovery failed: {e}")

        # 恢复信号历史
        trading_state = self.state.get("trading", {})
        if trading_state.get("last_signal"):
            recovery_info["signal_history"].append({
                "time": trading_state.get("last_signal_time", 0),
                "signal": trading_state["last_signal"]
            })

        return recovery_info


class WSMonitor:
    """主监控程序"""

    def __init__(self):
        self.ws_manager = WebSocketManager()
        # Per-coin signal processors
        self.signal_processors = {coin: SignalProcessor(coin) for coin in TRADING_COINS}
        self.trade_executor = TradeExecutor()
        self.notification_manager = NotificationManager()
        self.state_manager = StateManager()

        self.running = False
        self.tasks = []
        self._loop = None

        # 设置信号处理（优雅停机）
        sig.signal(sig.SIGTERM, self._signal_handler)
        sig.signal(sig.SIGINT, self._signal_handler)

    def _signal_handler(self, signum, frame):
        """信号处理器——通过 event loop 安全取消 tasks"""
        logger.info(f"Received signal {signum}, shutting down gracefully...")
        self.running = False
        if self._loop:
            for task in self.tasks:
                if not task.done():
                    self._loop.call_soon_threadsafe(task.cancel)
        else:
            # fallback: 直接 cancel（Python 中 task.cancel 只设标志位）
            for task in self.tasks:
                if not task.done():
                    task.cancel()

    async def start(self):
        """启动监控"""
        logger.info("Starting WebSocket Monitor...")
        self.running = True
        self._loop = asyncio.get_running_loop()

        # 恢复状态
        recovery = self.state_manager.recover_on_startup()
        if recovery["has_position"]:
            logger.info("Existing position detected, starting trailing monitor")
            # 已有仓位跳过 early validation（重启不应触发误平仓）
            from luckytrader.execute import get_position
            for coin in TRADING_COINS:
                if get_position(coin):
                    self.trade_executor._early_validation_done[coin] = True
                    logger.info(f"Skipping early validation for recovered {coin} position")
            await self.trade_executor.start_trailing_monitor()

        # 建立WebSocket连接
        if not await self.ws_manager.connect_with_retry():
            logger.error("Failed to establish WebSocket connection")
            return

        # 订阅所有交易币种
        await self.ws_manager.subscribe_all_coins()

        # 启动各种任务
        self.tasks = [
            asyncio.create_task(self._message_loop()),
            asyncio.create_task(self._heartbeat_monitor()),
            # 定时报告由 OpenClaw cron "市场报告 (30min)" 负责，ws_monitor 只发交易通知
        ]

        # 更新状态
        self.state_manager.update_websocket_status(True)
        self.state_manager.state["monitoring"]["start_time"] = datetime.now(timezone.utc).isoformat()
        self.state_manager.save_state(self.state_manager.state)

        logger.info("WebSocket Monitor started successfully")

        # 等待所有任务完成
        try:
            await asyncio.gather(*self.tasks)
        except asyncio.CancelledError:
            logger.info("Monitor tasks cancelled")

    async def _message_loop(self):
        """消息处理循环"""
        processed_count = 0

        while self.running:
            try:
                message = await self.ws_manager.receive_message()

                if message is None:
                    # 超时或连接问题
                    if not self.ws_manager.connected:
                        logger.warning("WebSocket disconnected, attempting reconnect...")
                        await asyncio.sleep(2)
                        if await self.ws_manager.reconnect_with_lock():
                            await self.ws_manager.subscribe_all_coins()
                    continue

                # 处理K线数据
                if message.get("channel") == "candle":
                    raw_data = message.get("data", {})
                    try:
                        kline_data = normalize_ws_kline(raw_data)
                    except (KeyError, TypeError) as e:
                        logger.error(f"Failed to normalize kline: {e} | raw={raw_data}")
                        continue

                    # Route to the correct per-coin signal processor
                    kline_coin = kline_data.get("coin", "BTC")
                    processor = self.signal_processors.get(kline_coin)
                    if not processor:
                        continue  # Unknown coin, skip

                    if processor.add_kline(kline_data):
                        processed_count += 1

                        # 检查 SL/TP 是否被自动触发（per-coin）
                        trigger_result = await self.trade_executor.check_position_closed_by_trigger(kline_coin)
                        if trigger_result:
                            logger.info(f"{kline_coin} SL/TP triggered: {trigger_result['reason']}, PnL {trigger_result['pnl_pct']:+.2f}%")
                            await self.notification_manager.async_notify_trade_closed(trigger_result)

                        # 信号检测
                        signal_result = processor.process_signal()

                        if signal_result and signal_result.get("signal") != "HOLD":
                            logger.info(f"{kline_coin} Signal detected: {signal_result['signal']}")
                            await self.notification_manager.async_notify_signal_detected(signal_result)

                            # 执行交易（per-coin）
                            trade_result = await self.trade_executor.execute_signal(signal_result, kline_coin)

                            if trade_result.get("action") == "OPENED":
                                self.state_manager.update_trading_status(f"{kline_coin}:{signal_result['signal']}")
                            elif trade_result.get("action") == "ERROR":
                                await self.notification_manager.async_notify_error(
                                    f"{kline_coin} Trade execution failed: {trade_result.get('error')}",
                                    critical=True)

                        # 更新处理计数
                        if processed_count % 100 == 0:
                            self.state_manager.state["monitoring"]["processed_messages"] = processed_count
                            self.state_manager.save_state(self.state_manager.state)

            except Exception as e:
                logger.error(f"Message processing error: {e}")
                await self.handle_error(e)
                await asyncio.sleep(1)  # 短暂暂停

    async def _heartbeat_monitor(self):
        """心跳监控任务"""
        await self.ws_manager.heartbeat_monitor()

    async def handle_error(self, error: Exception):
        """错误处理"""
        logger.error(f"Handling error: {error}")

        # 根据错误类型决定处理方式
        if isinstance(error, (ConnectionClosedError, WebSocketException)):
            logger.info("WebSocket error detected, triggering reconnect")
            await self.ws_manager.reconnect_with_lock()
            await self.ws_manager.subscribe_all_coins()

        elif "API" in str(error) or "timeout" in str(error).lower():
            # API相关错误
            await self.notification_manager.async_notify_error(f"API Error: {error}")

        else:
            # 其他错误
            await self.notification_manager.async_notify_error(f"System Error: {error}")

    def stop(self):
        """停止监控"""
        logger.info("Stopping WebSocket Monitor...")
        self.running = False

        # 取消所有任务
        for task in self.tasks:
            if not task.done():
                task.cancel()

        # 停止移动止损
        self.trade_executor.stop_trailing_monitor()

        # 更新状态
        self.state_manager.update_websocket_status(False)

    async def shutdown(self):
        """优雅停机"""
        self.stop()

        # 等待任务完成
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)

        # 关闭WebSocket连接
        await self.ws_manager.disconnect()

        logger.info("WebSocket Monitor shutdown complete")


async def main():
    """主函数"""
    monitor = WSMonitor()

    try:
        await monitor.start()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
    except Exception as e:
        logger.error(f"Monitor error: {e}")
        monitor.notification_manager.notify_critical_error(f"Monitor crashed: {e}")
    finally:
        await monitor.shutdown()


if __name__ == "__main__":
    # 设置异步运行环境
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    asyncio.run(main())
