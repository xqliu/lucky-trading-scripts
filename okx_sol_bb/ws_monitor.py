#!/usr/bin/env python3
"""
OKX SOL BB WebSocket Monitor — Mean-Reversion Close-Confirm Mode
==================================================================
Hardened WS monitor for SOL BB mean-reversion strategy.
Reuses battle-tested patterns from ETH BB monitor v2.3.

Safety guarantees:
1. Single-threaded: ALL REST calls via thread pool, state mutations in main loop.
2. _order_lock covers the ENTIRE cancel→check→place sequence.
3. Startup reconciliation before any orders.
4. SL health check every 5 minutes.
5. Watchdog: force-exit if no WS message for 5 min.

Strategy: Close-confirm mode only (no trigger orders).
  - On candle close: check if prev_close crossed back inside BB
  - If yes: market order entry → SL → TP
"""

import asyncio
import hashlib
import hmac
import base64
import json
import logging
import signal as sig
import sys
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List

import websockets
from websockets.exceptions import ConnectionClosedError, WebSocketException

_parent = str(Path(__file__).parent.parent)
if _parent not in sys.path:
    sys.path.insert(0, _parent)

from okx_sol_bb.config import load_config, OKXSolConfig
from okx_bb.exchange import OKXClient  # Reuse exchange wrapper
from okx_sol_bb.executor import SolBBExecutor, STATE_DIR
from okx_sol_bb.strategy import detect_signal, get_bb_levels
from core.state import load_state, save_state
from core.notify import send_discord as _send_discord_sync

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(name)s %(levelname)s %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)


async def send_discord(message: str, channel_id=None, mention: bool = False):
    try:
        loop = asyncio.get_running_loop()
        await asyncio.wait_for(
            loop.run_in_executor(None, lambda: _send_discord_sync(message, channel_id, mention)),
            timeout=15)
    except asyncio.TimeoutError:
        logger.error(f"send_discord timed out: {message[:100]}")
    except Exception as e:
        logger.error(f"send_discord failed: {e}")


WS_BUSINESS_URL = "wss://ws.okx.com:8443/ws/v5/business"
WS_PRIVATE_URL = "wss://ws.okx.com:8443/ws/v5/private"
PING_INTERVAL = 25
MAX_RECONNECT_DELAY = 120


class CandleAccumulator:
    def __init__(self, client: OKXClient, instId: str, max_bars: int = 500):
        self.client = client
        self.instId = instId
        self.max_bars = max_bars
        self.closes: List[float] = []
        self._initialized = False

    async def initialize(self, loop):
        candles = await loop.run_in_executor(
            None, lambda: self.client.get_candles(self.instId, bar="30m", limit=300))
        if not candles:
            logger.error("Failed to load historical candles!")
            return False
        self.closes = [c["c"] for c in candles]
        self._initialized = True
        logger.info(f"Loaded {len(self.closes)} candles, latest={self.closes[-1]:.2f}")
        return True

    def on_candle_close(self, close_price: float):
        self.closes.append(close_price)
        if len(self.closes) > self.max_bars:
            self.closes = self.closes[-self.max_bars:]

    @property
    def ready(self):
        return self._initialized and len(self.closes) >= 30  # BB(14) needs 15+ bars


class WSMonitor:
    def __init__(self, config: Optional[OKXSolConfig] = None):
        self.cfg = config or load_config()
        self.client = OKXClient(self.cfg.api_key, self.cfg.secret_key, self.cfg.passphrase)
        self.executor = SolBBExecutor(self.cfg)
        self.accumulator = CandleAccumulator(self.client, self.cfg.instId)

        self._running = False
        self._business_ws = None
        self._private_ws = None
        self._business_reconnect_delay = 1
        self._private_reconnect_delay = 1
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        self._entry_in_progress = False
        self._order_lock = asyncio.Lock()

        self._last_activity = time.time()
        self.WATCHDOG_TIMEOUT = 300

        self._thread_local = threading.local()

    def _get_thread_client(self) -> OKXClient:
        if not hasattr(self._thread_local, 'client'):
            self._thread_local.client = OKXClient(
                self.cfg.api_key, self.cfg.secret_key, self.cfg.passphrase)
        return self._thread_local.client

    async def _rest(self, fn, *args, **kwargs):
        def _run():
            tc = self._get_thread_client()
            if hasattr(fn, '__self__') and isinstance(fn.__self__, OKXClient):
                return getattr(tc, fn.__name__)(*args, **kwargs)
            return fn(*args, **kwargs)
        return await self._loop.run_in_executor(None, _run)

    async def _rest_exchange(self, method_name: str, *args, **kwargs):
        def _run():
            tc = self._get_thread_client()
            return getattr(tc, method_name)(*args, **kwargs)
        try:
            return await asyncio.wait_for(
                self._loop.run_in_executor(None, _run), timeout=30)
        except asyncio.TimeoutError:
            logger.error(f"REST call {method_name} timed out!")
            raise

    # === Startup Reconciliation ===

    async def _reconcile_on_startup(self):
        logger.info("Startup reconciliation...")

        positions = await self._rest_exchange("get_positions", self.cfg.instId)
        if positions is None:
            logger.error("Cannot check positions")
            return

        has_position = any(float(p.get("pos", 0)) != 0 for p in positions)

        if has_position:
            pos_info = next(p for p in positions if float(p.get("pos", 0)) != 0)
            pos_val = float(pos_info.get("pos", 0))
            direction = "LONG" if pos_val > 0 else "SHORT"
            avg_px = float(pos_info.get("avgPx", 0))
            pos_size = abs(pos_val)

            logger.info(f"Position found: {direction} {pos_size} @ {avg_px}")

            algos_cond = await self._rest_exchange("get_algo_orders", self.cfg.instId, "conditional")
            has_sl = any(a.get("slTriggerPx") for a in algos_cond)

            if not has_sl:
                logger.error("Position has NO SL! Re-setting...")
                close_side = "sell" if direction == "LONG" else "buy"
                if direction == "LONG":
                    sl_p = avg_px * (1 - self.cfg.risk.stop_loss_pct)
                    tp_p = avg_px * (1 + self.cfg.risk.take_profit_pct)
                else:
                    sl_p = avg_px * (1 + self.cfg.risk.stop_loss_pct)
                    tp_p = avg_px * (1 - self.cfg.risk.take_profit_pct)

                sl_result = await self._rest_exchange(
                    "place_stop_order", self.cfg.instId, close_side, f"{pos_size:.2f}",
                    slTriggerPx=f"{sl_p:.2f}")

                if sl_result.get("code") != "0" or not sl_result.get("data"):
                    logger.error(f"SL re-set FAILED — EMERGENCY CLOSE!")
                    await self._rest_exchange("place_market_order",
                        self.cfg.instId, close_side, f"{pos_size:.2f}", True)
                    await send_discord(f"🚨 SOL BB 启动发现裸仓且SL失败 → 紧急平仓", mention=True)
                    self.executor.save_position(None)
                else:
                    sl_algo_id = sl_result["data"][0].get("algoId", "")
                    tp_result = await self._rest_exchange(
                        "place_limit_order", self.cfg.instId, close_side, f"{pos_size:.2f}",
                        px=f"{tp_p:.2f}", reduceOnly=True)
                    tp_id = tp_result.get("data", [{}])[0].get("ordId", "") if tp_result.get("code") == "0" else ""

                    pos_state = {
                        "direction": direction, "entry_price": avg_px,
                        "size": f"{pos_size:.2f}", "sl_price": sl_p, "tp_price": tp_p,
                        "sl_algo_id": sl_algo_id, "tp_order_id": tp_id,
                        "entry_time": datetime.now(timezone.utc).isoformat(),
                        "entry_bar_count": 0,
                    }
                    self.executor.save_position(pos_state)
                    self.executor._append_open_trade_log_if_missing(pos_state, source="startup_reconcile")
                    await send_discord(f"⚠️ SOL BB 启动恢复: {direction} @ ${avg_px:.2f}\nSL: ${sl_p:.2f} / TP: ${tp_p:.2f}", mention=True)
            else:
                self.executor.reconcile_position_from_exchange(source="startup_reconcile")
                logger.info("Position has SL — state synced")

        logger.info("Reconciliation complete")

    # === Entry Logic ===

    async def _on_entry_filled(self, direction: str, fill_price: float, fill_sz: str):
        self._entry_in_progress = True
        try:
            await self._on_entry_filled_inner(direction, fill_price, fill_sz)
        except Exception as e:
            logger.error(f"CRITICAL: _on_entry_filled exception: {e}", exc_info=True)
            try:
                positions = await self._rest_exchange("get_positions", self.cfg.instId)
                if positions and any(float(p.get("pos", 0)) != 0 for p in positions):
                    pos_info = next(p for p in positions if float(p.get("pos", 0)) != 0)
                    pv = float(pos_info.get("pos", 0))
                    close_side = "sell" if pv > 0 else "buy"
                    algos = await self._rest_exchange("get_algo_orders", self.cfg.instId, "conditional")
                    if not any(a.get("slTriggerPx") for a in algos):
                        await self._rest_exchange("place_market_order",
                            self.cfg.instId, close_side, f"{abs(pv):.2f}", True)
                        self.executor.save_position(None)
                        await send_discord(f"🚨 SOL BB 入场异常且无SL → 紧急平仓\n{e}", mention=True)
            except Exception as e2:
                await send_discord(f"🚨🚨 SOL BB 双重异常！手动检查！\n{e}\n{e2}", mention=True)
        finally:
            self._entry_in_progress = False

    async def _on_entry_filled_inner(self, direction: str, fill_price: float, fill_sz: str):
        logger.info(f"🎯 Entry: {direction} @ {fill_price:.2f} sz={fill_sz}")

        if fill_price <= 0:
            positions = await self._rest_exchange("get_positions", self.cfg.instId)
            if positions:
                for p in positions:
                    if float(p.get("pos", 0)) != 0:
                        fill_price = float(p.get("avgPx", 0))
                        break
            if fill_price <= 0:
                close_side = "sell" if direction == "LONG" else "buy"
                await self._rest_exchange("place_market_order",
                    self.cfg.instId, close_side, fill_sz, True)
                await send_discord("🚨 SOL BB 入场价格无效，紧急平仓", mention=True)
                return

        # Get actual size from exchange
        positions = await self._rest_exchange("get_positions", self.cfg.instId)
        actual_sz = fill_sz
        if positions:
            for p in positions:
                pv = abs(float(p.get("pos", 0)))
                if pv > 0:
                    actual_sz = f"{pv:.2f}"
                    break

        close_side = "sell" if direction == "LONG" else "buy"

        if direction == "LONG":
            sl_price = fill_price * (1 - self.cfg.risk.stop_loss_pct)
            tp_price = fill_price * (1 + self.cfg.risk.take_profit_pct)
        else:
            sl_price = fill_price * (1 + self.cfg.risk.stop_loss_pct)
            tp_price = fill_price * (1 - self.cfg.risk.take_profit_pct)

        # SL — CRITICAL
        sl_result = await self._rest_exchange(
            "place_stop_order", self.cfg.instId, close_side, actual_sz,
            slTriggerPx=f"{sl_price:.2f}")

        if sl_result.get("code") != "0" or not sl_result.get("data"):
            logger.error(f"SL FAILED — EMERGENCY CLOSE!")
            await self._rest_exchange("place_market_order",
                self.cfg.instId, close_side, actual_sz, True)
            await send_discord("🚨 SOL BB 止损设置失败，紧急平仓", mention=True)
            self.executor.save_position(None)
            return

        sl_algo_id = sl_result["data"][0].get("algoId", "")

        # Verify SL is live
        await asyncio.sleep(1)
        algos = await self._rest_exchange("get_algo_orders", self.cfg.instId, "conditional")
        if not any(a.get("algoId") == sl_algo_id for a in algos):
            logger.error("SL not live — emergency close!")
            await self._rest_exchange("place_market_order",
                self.cfg.instId, close_side, actual_sz, True)
            await send_discord("🚨 SOL BB 止损未激活，紧急平仓", mention=True)
            self.executor.save_position(None)
            return

        # TP
        tp_result = await self._rest_exchange(
            "place_limit_order", self.cfg.instId, close_side, actual_sz,
            px=f"{tp_price:.2f}", reduceOnly=True)
        tp_ord_id = ""
        if tp_result.get("code") == "0" and tp_result.get("data"):
            tp_ord_id = tp_result["data"][0].get("ordId", "")
        else:
            await send_discord("⚠️ SOL BB TP设置失败，仅有SL保护")

        pos_state = {
            "direction": direction, "entry_price": fill_price,
            "size": actual_sz, "sl_price": sl_price, "tp_price": tp_price,
            "sl_algo_id": sl_algo_id, "tp_order_id": tp_ord_id,
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "entry_bar_count": 0,
        }
        self.executor.save_position(pos_state)
        self.executor._append_open_trade_log_if_missing(pos_state, source="entry_fill")

        await send_discord(
            f"📊 OKX SOL BB: {direction} {self.cfg.coin}\n"
            f"入场: ${fill_price:.2f}\n"
            f"止损: ${sl_price:.2f} ({self.cfg.risk.stop_loss_pct*100:.1f}%)\n"
            f"止盈: ${tp_price:.2f} ({self.cfg.risk.take_profit_pct*100:.1f}%)\n"
            f"合约: {actual_sz}",
            mention=True)

    # === Trading Logic ===

    async def _on_candle_close(self):
        try:
            result = await self._rest(self.executor.check_position)
            if result:
                logger.info(f"Position closed: {result.exit_reason.value}")
                await send_discord(
                    f"📊 OKX SOL BB 平仓: {result.exit_reason.value}\n"
                    f"{result.direction.value} {result.coin}\n"
                    f"入场: ${result.entry_price:.2f} → 出场: ${result.exit_price:.2f}\n"
                    f"PnL: {result.pnl_pct*100:+.2f}%",
                    mention=True)
        except Exception as e:
            logger.error(f"check_position error: {e}", exc_info=True)

        if not self.executor.load_position():
            await self._close_confirm_entry()

    async def _close_confirm_entry(self):
        """Close-confirm mean-reversion entry."""
        async with self._order_lock:
            if self._entry_in_progress:
                return

            if not self.accumulator.ready:
                return

            closes = self.accumulator.closes
            idx = len(closes) - 1

            signal = detect_signal(
                closes,
                bb_period=self.cfg.strategy.bb_period,
                bb_mult=self.cfg.strategy.bb_multiplier,
                idx=idx)

            if not signal:
                bb = get_bb_levels(closes, self.cfg.strategy.bb_period,
                                   self.cfg.strategy.bb_multiplier, idx)
                if bb:
                    mid, upper, lower = bb
                    logger.info(f"No signal: close={closes[-1]:.2f} "
                                f"BB=[{lower:.2f}, {mid:.2f}, {upper:.2f}]")
                return

            logger.info(f"📊 Mean-reversion signal: {signal} @ close={closes[-1]:.2f}")

            # Verify no position
            positions = await self._rest_exchange("get_positions", self.cfg.instId)
            if positions is None:
                logger.warning("Can't verify positions, skipping")
                return
            if any(float(p.get("pos", 0)) != 0 for p in positions):
                return

            sz = await self._rest(self.executor.calculate_size)
            if not sz:
                return

            direction = "buy" if signal == "LONG" else "sell"

            result = await self._rest_exchange(
                "place_market_order", self.cfg.instId, direction, sz)

            if result.get("code") == "0" and result.get("data"):
                ord_id = result["data"][0].get("ordId", "")
                logger.info(f"Market order placed: {direction} sz={sz} ordId={ord_id}")
                await asyncio.sleep(2)
                positions = await self._rest_exchange("get_positions", self.cfg.instId)
                if positions and any(float(p.get("pos", 0)) != 0 for p in positions):
                    pos_info = next(p for p in positions if float(p.get("pos", 0)) != 0)
                    fill_price = float(pos_info.get("avgPx", closes[-1]))
                    fill_sz = f"{abs(float(pos_info.get('pos', 0))):.2f}"
                    await self._on_entry_filled(signal, fill_price, fill_sz)
                else:
                    logger.error(f"Market order sent but no position! ordId={ord_id}")
                    await send_discord(f"⚠️ SOL BB 市价单无持仓 ordId={ord_id}", mention=True)
            else:
                logger.error(f"Market order failed: {result}")
                await send_discord(f"⚠️ SOL BB 市价开仓失败: {result}", mention=True)

    async def _check_position_closed(self):
        await asyncio.sleep(2)
        try:
            result = await self._rest(self.executor.check_position)
            if result:
                await send_discord(
                    f"📊 OKX SOL BB 平仓: {result.exit_reason.value}\n"
                    f"{result.direction.value} {result.coin}\n"
                    f"入场: ${result.entry_price:.2f} → 出场: ${result.exit_price:.2f}\n"
                    f"PnL: {result.pnl_pct*100:+.2f}%",
                    mention=True)
        except Exception as e:
            logger.error(f"check error: {e}", exc_info=True)

    # === WebSocket Connection ===

    def _ws_sign(self):
        ts = str(int(time.time()))
        msg = ts + "GET" + "/users/self/verify"
        mac = hmac.new(self.cfg.secret_key.encode(), msg.encode(), hashlib.sha256)
        return {"op": "login", "args": [{
            "apiKey": self.cfg.api_key, "passphrase": self.cfg.passphrase,
            "timestamp": ts, "sign": base64.b64encode(mac.digest()).decode(),
        }]}

    async def _connect_business(self):
        try:
            self._business_ws = await websockets.connect(
                WS_BUSINESS_URL, ping_interval=PING_INTERVAL, ping_timeout=10, close_timeout=5)
            await self._business_ws.send(json.dumps({
                "op": "subscribe",
                "args": [{"channel": "candle30m", "instId": self.cfg.instId}]
            }))
            logger.info(f"Business WS connected, candle30m {self.cfg.instId}")
            self._business_reconnect_delay = 1
            return True
        except Exception as e:
            logger.error(f"Business WS failed: {e}")
            return False

    async def _connect_private(self):
        try:
            self._private_ws = await websockets.connect(
                WS_PRIVATE_URL, ping_interval=PING_INTERVAL, ping_timeout=10, close_timeout=5)
            await self._private_ws.send(json.dumps(self._ws_sign()))
            resp = await asyncio.wait_for(self._private_ws.recv(), timeout=10)
            data = json.loads(resp)
            if not (data.get("event") == "login" and data.get("code") == "0"):
                logger.error(f"Private WS login failed: {data}")
                return False
            for ch in ["orders", "orders-algo"]:
                await self._private_ws.send(json.dumps({
                    "op": "subscribe",
                    "args": [{"channel": ch, "instType": "SWAP"}]
                }))
            logger.info("Private WS connected, orders + orders-algo")
            self._private_reconnect_delay = 1
            return True
        except Exception as e:
            logger.error(f"Private WS failed: {e}")
            return False

    # === Message Handlers ===

    async def _handle_business_message(self, msg: str):
        self._last_activity = time.time()
        try:
            data = json.loads(msg)
        except json.JSONDecodeError:
            return
        if data.get("event") == "subscribe":
            return
        arg = data.get("arg", {})
        if arg.get("channel", "").startswith("candle") and "data" in data:
            for candle in data["data"]:
                if len(candle) >= 9 and candle[8] == "1":
                    close = float(candle[4])
                    ts = int(candle[0])
                    logger.info(f"Candle closed: {close:.2f} at "
                                f"{datetime.fromtimestamp(ts/1000, tz=timezone.utc)}")
                    self.accumulator.on_candle_close(close)
                    await self._on_candle_close()

    async def _handle_private_message(self, msg: str):
        self._last_activity = time.time()
        try:
            data = json.loads(msg)
        except json.JSONDecodeError:
            return
        if data.get("event"):
            return

        channel = data.get("arg", {}).get("channel", "")

        if channel == "orders-algo" and "data" in data:
            for order in data["data"]:
                state = order.get("state", "")
                if state in ("effective", "filled"):
                    # SL/TP triggered — check if position closed
                    await self._check_position_closed()

        elif channel == "orders" and "data" in data:
            for order in data["data"]:
                if order.get("state") == "filled":
                    if self.executor.load_position():
                        await self._check_position_closed()

    # === Periodic Check ===

    async def _periodic_check(self):
        while self._running:
            await asyncio.sleep(300)
            try:
                # Watchdog
                idle_secs = time.time() - self._last_activity
                if idle_secs > self.WATCHDOG_TIMEOUT:
                    logger.error(f"WATCHDOG: No activity for {idle_secs:.0f}s — FORCE EXIT!")
                    try:
                        _send_discord_sync(f"🚨 SOL BB Watchdog: {idle_secs:.0f}s 无WS消息，强制退出", mention=True)
                    except Exception:
                        pass
                    import os
                    os._exit(1)

                if self._entry_in_progress:
                    continue

                # Check position timeout
                result = await self._rest(self.executor.check_position)
                if result:
                    await send_discord(
                        f"📊 SOL BB 平仓 (periodic): {result.exit_reason.value}\n"
                        f"{result.direction.value} @ ${result.entry_price:.2f} → ${result.exit_price:.2f}\n"
                        f"PnL: {result.pnl_pct*100:+.2f}%",
                        mention=True)
                    continue

                # SL health check
                local_pos = self.executor.load_position()
                if local_pos and not self._entry_in_progress:
                    algos = await self._rest_exchange("get_algo_orders", self.cfg.instId, "conditional")
                    has_sl = any(a.get("slTriggerPx") for a in (algos or []))
                    if not has_sl:
                        logger.error("PERIODIC: NO SL on exchange! Re-setting...")

                        # Use EXCHANGE position (not local) to prevent size mismatch
                        positions = await self._rest_exchange("get_positions", self.cfg.instId)
                        if not positions or not any(float(p.get("pos", 0)) != 0 for p in positions):
                            logger.warning("PERIODIC: SL missing but position closed on exchange")
                            await self._rest(self.executor.check_position)
                            continue

                        pos_info = next(p for p in positions if float(p.get("pos", 0)) != 0)
                        exchange_size = abs(float(pos_info.get("pos", 0)))
                        exchange_dir = "LONG" if float(pos_info.get("pos", 0)) > 0 else "SHORT"
                        exchange_avg = float(pos_info.get("avgPx", 0) or 0)

                        # Sync if diverged
                        local_dir = local_pos.get("direction", "SHORT")
                        local_sz = float(local_pos.get("size", 0) or 0)
                        if exchange_dir != local_dir or abs(exchange_size - local_sz) > 0.001:
                            logger.error(f"PERIODIC: Mismatch local={local_dir} {local_sz} vs exchange={exchange_dir} {exchange_size}")
                            self.executor.reconcile_position_from_exchange(source="periodic_sl_mismatch")
                            local_pos = self.executor.load_position()
                            if not local_pos:
                                continue

                        d = exchange_dir
                        ap = exchange_avg
                        sz = f"{exchange_size:.2f}"
                        close_side = "sell" if d == "LONG" else "buy"
                        if d == "LONG":
                            sl_p = ap * (1 - self.cfg.risk.stop_loss_pct)
                        else:
                            sl_p = ap * (1 + self.cfg.risk.stop_loss_pct)

                        # Check if price already past SL — market close instead
                        ticker = await self._rest_exchange("get_ticker", self.cfg.instId)
                        if ticker:
                            current_price = ticker.get("last", 0)
                            if (d == "LONG" and current_price <= sl_p) or \
                               (d == "SHORT" and current_price >= sl_p):
                                logger.error(f"PERIODIC: Price {current_price} past SL {sl_p:.2f} — market close")
                                await self._rest_exchange("place_market_order",
                                    self.cfg.instId, close_side, sz, True)
                                await send_discord(f"🚨 SOL BB 价格已穿 SL → 市价平仓", mention=True)
                                self.executor.save_position(None)
                                continue

                        sl_result = await self._rest_exchange(
                            "place_stop_order", self.cfg.instId, close_side, sz,
                            slTriggerPx=f"{sl_p:.2f}")

                        if sl_result and sl_result.get("code") == "0" and sl_result.get("data"):
                            local_pos["sl_algo_id"] = sl_result["data"][0].get("algoId", "")
                            local_pos["sl_price"] = sl_p
                            local_pos["size"] = sz
                            local_pos["direction"] = d
                            local_pos["entry_price"] = ap
                            self.executor.save_position(local_pos)
                            await send_discord(f"⚠️ SOL BB SL 丢失 → 已重设 ${sl_p:.2f} sz={sz}", mention=True)
                        else:
                            await self._rest_exchange("place_market_order",
                                self.cfg.instId, close_side, sz, True)
                            await send_discord("🚨 SOL BB SL 重设失败 → 紧急平仓", mention=True)
                            self.executor.save_position(None)

                # Orphan detection
                if not local_pos and not self._entry_in_progress:
                    positions = await self._rest_exchange("get_positions", self.cfg.instId)
                    if positions and any(float(p.get("pos", 0)) != 0 for p in positions):
                        if self._entry_in_progress:
                            continue
                        logger.error("PERIODIC: Orphan detected!")
                        self.executor.reconcile_position_from_exchange(source="periodic_orphan")
                        await send_discord("⚠️ SOL BB 发现孤立仓位，已恢复", mention=True)

            except Exception as e:
                logger.error(f"Periodic error: {e}", exc_info=True)

    # === Main Loops ===

    def _ws_is_open(self, ws):
        if ws is None:
            return False
        try:
            return ws.state.name == "OPEN"
        except Exception:
            return False

    async def _business_loop(self):
        while self._running:
            try:
                if not self._ws_is_open(self._business_ws):
                    if not await self._connect_business():
                        await asyncio.sleep(self._business_reconnect_delay)
                        self._business_reconnect_delay = min(
                            self._business_reconnect_delay * 2, MAX_RECONNECT_DELAY)
                        continue
                    await self.accumulator.initialize(self._loop)

                msg = await asyncio.wait_for(self._business_ws.recv(), timeout=60)
                await self._handle_business_message(msg)

            except asyncio.TimeoutError:
                pass
            except (ConnectionClosedError, WebSocketException) as e:
                logger.warning(f"Business WS disconnected: {e}")
                self._business_ws = None
                await asyncio.sleep(self._business_reconnect_delay)
                self._business_reconnect_delay = min(
                    self._business_reconnect_delay * 2, MAX_RECONNECT_DELAY)
            except Exception as e:
                logger.error(f"Business WS error: {e}", exc_info=True)
                await asyncio.sleep(5)

    async def _private_loop(self):
        while self._running:
            try:
                if not self._ws_is_open(self._private_ws):
                    if not await self._connect_private():
                        await asyncio.sleep(self._private_reconnect_delay)
                        self._private_reconnect_delay = min(
                            self._private_reconnect_delay * 2, MAX_RECONNECT_DELAY)
                        continue

                msg = await asyncio.wait_for(self._private_ws.recv(), timeout=60)
                await self._handle_private_message(msg)

            except asyncio.TimeoutError:
                pass
            except (ConnectionClosedError, WebSocketException) as e:
                logger.warning(f"Private WS disconnected: {e}")
                self._private_ws = None
                await asyncio.sleep(self._private_reconnect_delay)
            except Exception as e:
                logger.error(f"Private WS error: {e}", exc_info=True)
                await asyncio.sleep(5)

    async def run(self):
        self._loop = asyncio.get_event_loop()

        logger.info("=" * 60)
        logger.info("OKX SOL BB Monitor — Mean-Reversion")
        logger.info(f"{self.cfg.instId} BB({self.cfg.strategy.bb_period}, "
                     f"{self.cfg.strategy.bb_multiplier}) "
                     f"TP={self.cfg.risk.take_profit_pct*100}% "
                     f"SL={self.cfg.risk.stop_loss_pct*100}%")
        logger.info("=" * 60)

        if not await self.accumulator.initialize(self._loop):
            await asyncio.sleep(30)
            if not await self.accumulator.initialize(self._loop):
                raise RuntimeError("Candle init failed")

        self._running = True

        for s in (sig.SIGINT, sig.SIGTERM):
            self._loop.add_signal_handler(s, self._shutdown)

        await self._connect_business()
        await self._connect_private()
        await self._reconcile_on_startup()

        # Set leverage
        lev_result = await self._rest_exchange(
            "set_leverage", self.cfg.instId, str(self.cfg.risk.leverage), "isolated")
        if isinstance(lev_result, dict) and lev_result.get("code") != "0":
            logger.warning(f"set_leverage: {lev_result.get('msg', lev_result)}")

        import subprocess as _sp
        from datetime import timedelta
        try:
            _raw = _sp.run(
                ["git", "log", "--format=%h %s|%ct", "-1"],
                capture_output=True, text=True, timeout=5,
                cwd=str(Path(__file__).parent.parent)).stdout.strip()
            _parts = _raw.rsplit("|", 1)
            _sgt = datetime.fromtimestamp(int(_parts[1]), tz=timezone(timedelta(hours=8)))
            _commit = f"{_parts[0]} ({_sgt:%Y-%m-%d %H:%M} SGT)"
        except Exception:
            _commit = "unknown"

        await send_discord(
            f"🟢 OKX SOL BB 启动\n"
            f"{self.cfg.instId} BB({self.cfg.strategy.bb_period}, "
            f"{self.cfg.strategy.bb_multiplier})\n"
            f"TP={self.cfg.risk.take_profit_pct*100}% SL={self.cfg.risk.stop_loss_pct*100}%\n"
            f"版本: {_commit}")

        await asyncio.gather(
            self._business_loop(),
            self._private_loop(),
            self._periodic_check(),
        )

    def _shutdown(self):
        logger.info("Shutdown signal received")
        self._running = False
        try:
            _send_discord_sync("🔴 OKX SOL BB Monitor 停止")
        except Exception:
            pass


def main():
    monitor = WSMonitor()
    asyncio.run(monitor.run())


if __name__ == "__main__":
    main()
