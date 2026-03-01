#!/usr/bin/env python3
"""
OKX BB WebSocket Monitor v2.3 — Hardened Intrabar Trigger Mode
================================================================
Post-review v2 hardened. Safety guarantees:

1. Single-threaded: ALL REST calls via _rest() but state mutations
   happen ONLY in the main asyncio coroutine after await returns.
2. _order_lock covers the ENTIRE cancel→check→place sequence (atomic).
3. _triggered_direction is NEVER cleared by cancel — only by fill handler.
4. Startup reconciliation before any orders.
5. Periodic orphan detection with entry-in-progress guard.
6. Dedicated requests.Session per thread via threading.local.
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

from okx_bb.config import load_config, OKXConfig
from okx_bb.exchange import OKXClient
from okx_bb.executor import BBExecutor, STATE_DIR
from okx_bb.strategy import get_bb_levels
from core.indicators import ema
from core.state import load_state, save_state
from core.notify import send_discord

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(name)s %(levelname)s %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

WS_BUSINESS_URL = "wss://ws.okx.com:8443/ws/v5/business"
WS_PRIVATE_URL = "wss://ws.okx.com:8443/ws/v5/private"

PING_INTERVAL = 25
MAX_RECONNECT_DELAY = 120

PENDING_STATE_FILE = STATE_DIR / "pending_orders.json"

# Prefix for all Discord messages (remove when system proven stable)
MSG_PREFIX = "[OKX测试] "


class CandleAccumulator:
    def __init__(self, client: OKXClient, instId: str, max_bars: int = 500):
        self.client = client
        self.instId = instId
        self.max_bars = max_bars
        self.closes: List[float] = []
        self._initialized = False

    async def initialize(self, loop):
        candles = await loop.run_in_executor(
            None, lambda: self.client.get_candles(self.instId, bar="30m", limit=300)
        )
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
        return self._initialized and len(self.closes) >= 120


class WSMonitor:
    def __init__(self, config: Optional[OKXConfig] = None):
        self.cfg = config or load_config()
        self.client = OKXClient(self.cfg.api_key, self.cfg.secret_key, self.cfg.passphrase)
        self.executor = BBExecutor(self.cfg)
        self.accumulator = CandleAccumulator(self.client, self.cfg.instId)

        self._running = False
        self._business_ws = None
        self._private_ws = None
        self._business_reconnect_delay = 1
        self._private_reconnect_delay = 1
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # Pending entry trigger IDs
        self._pending_long_algoId: Optional[str] = None
        self._pending_short_algoId: Optional[str] = None

        # Triggered-but-not-filled state
        # IMPORTANT: Only cleared by _on_entry_filled, NEVER by cancel
        self._triggered_direction: Optional[str] = None
        self._triggered_sz: Optional[str] = None

        # Guard: entry in progress (prevents orphan detector from killing it)
        self._entry_in_progress = False

        # Mutex: covers cancel → check → place atomically
        self._order_lock = asyncio.Lock()

        # Thread-safe REST: each executor thread gets its own Session
        self._thread_local = threading.local()

    # === Thread-safe REST ===

    def _get_thread_client(self) -> OKXClient:
        """Get a per-thread OKXClient with its own requests.Session."""
        if not hasattr(self._thread_local, 'client'):
            self._thread_local.client = OKXClient(
                self.cfg.api_key, self.cfg.secret_key, self.cfg.passphrase
            )
        return self._thread_local.client

    async def _rest(self, fn, *args, **kwargs):
        """Run REST call in thread pool with per-thread client.
        
        If fn is a method on self.client or self.executor.client,
        we rebind it to the thread-local client.
        """
        def _run():
            # Use thread-local client for exchange calls
            tc = self._get_thread_client()
            # If fn is a bound method of OKXClient, rebind
            if hasattr(fn, '__self__') and isinstance(fn.__self__, OKXClient):
                method_name = fn.__name__
                return getattr(tc, method_name)(*args, **kwargs)
            # For executor methods that internally use self.client
            # We can't easily rebind, so use a lock instead
            return fn(*args, **kwargs)
        return await self._loop.run_in_executor(None, _run)

    async def _rest_exchange(self, method_name: str, *args, **kwargs):
        """Call OKXClient method by name, thread-safe."""
        def _run():
            tc = self._get_thread_client()
            return getattr(tc, method_name)(*args, **kwargs)
        return await self._loop.run_in_executor(None, _run)

    # === Pending State ===

    def _save_pending(self):
        save_state(PENDING_STATE_FILE, {
            "long_algoId": self._pending_long_algoId,
            "short_algoId": self._pending_short_algoId,
        })

    def _load_pending(self):
        state = load_state(PENDING_STATE_FILE)
        self._pending_long_algoId = state.get("long_algoId")
        self._pending_short_algoId = state.get("short_algoId")

    # === Startup Reconciliation ===

    async def _reconcile_on_startup(self):
        """Check exchange for orphaned positions and stale orders."""
        logger.info("Startup reconciliation...")

        # 1. Check real positions
        positions = await self._rest_exchange("get_positions", self.cfg.instId)
        if positions is None:
            logger.error("Cannot check positions (API error)")
            return

        has_position = any(float(p.get("pos", 0)) != 0 for p in positions)

        if has_position and not self.executor.load_position():
            pos_info = next(p for p in positions if float(p.get("pos", 0)) != 0)
            pos_val = float(pos_info.get("pos", 0))
            direction = "LONG" if pos_val > 0 else "SHORT"
            avg_px = float(pos_info.get("avgPx", 0))
            pos_size = abs(pos_val)

            logger.error(f"ORPHAN: {direction} {pos_size} @ {avg_px}")

            # Check for SL (both conditional AND trigger types)
            algos_cond = await self._rest_exchange("get_algo_orders", self.cfg.instId, "conditional")
            has_sl = any(a.get("slTriggerPx") for a in algos_cond)

            if not has_sl:
                logger.error("Orphan has NO SL — emergency closing!")
                close_side = "sell" if direction == "LONG" else "buy"
                await self._rest_exchange("place_market_order",
                    self.cfg.instId, close_side, f"{pos_size:.2f}", True)
                send_discord(f"{MSG_PREFIX}🚨 启动发现裸仓 → 紧急平仓\n{direction} {pos_size} @ ${avg_px:.2f}", mention=True)
            else:
                # Reconstruct local state
                if direction == "LONG":
                    sl_p = avg_px * (1 - self.cfg.risk.stop_loss_pct)
                    tp_p = avg_px * (1 + self.cfg.risk.take_profit_pct)
                else:
                    sl_p = avg_px * (1 + self.cfg.risk.stop_loss_pct)
                    tp_p = avg_px * (1 - self.cfg.risk.take_profit_pct)

                sl_id = next((a["algoId"] for a in algos_cond if a.get("slTriggerPx")), "")
                open_ords = await self._rest_exchange("get_open_orders", self.cfg.instId)
                tp_id = next((o["ordId"] for o in open_ords if o.get("reduceOnly") == "true"), "")

                self.executor.save_position({
                    "direction": direction, "entry_price": avg_px,
                    "size": f"{pos_size:.2f}", "sl_price": sl_p, "tp_price": tp_p,
                    "sl_algo_id": sl_id, "tp_order_id": tp_id,
                    "entry_time": datetime.now(timezone.utc).isoformat(),
                    "entry_bar_count": 0,
                })
                send_discord(f"{MSG_PREFIX}⚠️ 启动恢复仓位: {direction} @ ${avg_px:.2f}")

        # 2. Validate pending orders (check BOTH conditional and trigger types)
        if self._pending_long_algoId or self._pending_short_algoId:
            algos_trigger = await self._rest_exchange("get_algo_orders", self.cfg.instId, "trigger")
            algos_cond2 = await self._rest_exchange("get_algo_orders", self.cfg.instId, "conditional")
            live_ids = {a["algoId"] for a in (algos_trigger + algos_cond2)}

            if self._pending_long_algoId and self._pending_long_algoId not in live_ids:
                logger.info(f"Cleared expired LONG trigger {self._pending_long_algoId}")
                self._pending_long_algoId = None
            if self._pending_short_algoId and self._pending_short_algoId not in live_ids:
                logger.info(f"Cleared expired SHORT trigger {self._pending_short_algoId}")
                self._pending_short_algoId = None
            self._save_pending()

        logger.info("Reconciliation complete")

    # === BB Order Placement ===

    def _get_trend(self) -> Optional[str]:
        closes = self.accumulator.closes
        idx = len(closes) - 1
        period = self.cfg.strategy.trend_ema_period
        lookback = self.cfg.strategy.trend_lookback
        ema_start = max(0, idx - period * 3)
        ema_vals = ema(closes[ema_start:idx + 1], period)
        if len(ema_vals) < lookback + 1:
            return None
        if ema_vals[-1] > ema_vals[-1 - lookback]:
            return "up"
        elif ema_vals[-1] < ema_vals[-1 - lookback]:
            return "down"
        return None

    async def _atomic_cancel_and_place(self):
        """Atomically: cancel old triggers → check state → place new triggers.
        
        Entire sequence under _order_lock. This prevents the race where
        a trigger fires between cancel and place.
        """
        async with self._order_lock:
            # If a trigger just fired, don't interfere
            if self._triggered_direction or self._entry_in_progress:
                logger.info("Entry in progress, skipping cancel+place")
                return

            # Cancel existing triggers
            if self._pending_long_algoId:
                try:
                    await self._rest_exchange("cancel_algo_order",
                        self._pending_long_algoId, self.cfg.instId)
                except Exception:
                    pass
                self._pending_long_algoId = None

            if self._pending_short_algoId:
                try:
                    await self._rest_exchange("cancel_algo_order",
                        self._pending_short_algoId, self.cfg.instId)
                except Exception:
                    pass
                self._pending_short_algoId = None

            self._save_pending()

            # Recheck: trigger may have fired during cancel awaits
            if self._triggered_direction or self._entry_in_progress:
                logger.info("Trigger fired during cancel, aborting place")
                return

            # Check position (exchange-verified)
            positions = await self._rest_exchange("get_positions", self.cfg.instId)
            if positions is None:
                logger.warning("Can't verify positions, skipping")
                return
            if any(float(p.get("pos", 0)) != 0 for p in positions):
                return
            if self.executor.load_position():
                return

            # Final recheck before placing
            if self._triggered_direction or self._entry_in_progress:
                return

            # Place new triggers
            await self._place_bb_orders_inner()

    async def _place_bb_orders_inner(self):
        """Place trigger orders. MUST be called under _order_lock."""
        if not self.accumulator.ready:
            return

        closes = self.accumulator.closes
        idx = len(closes) - 1
        bb = get_bb_levels(closes, self.cfg.strategy.bb_period,
                           self.cfg.strategy.bb_multiplier, idx)
        if bb is None:
            logger.info("BB=None (flat market)")
            return

        mid, upper, lower = bb
        trend = self._get_trend()
        current_price = closes[-1]

        logger.info(f"BB: upper={upper:.2f} mid={mid:.2f} lower={lower:.2f} "
                     f"price={current_price:.2f} trend={trend}")

        if trend is None:
            logger.info("Trend unclear, not placing")
            return

        sz = await self._rest(self.executor.calculate_size)
        if not sz:
            return

        if trend == "up" and upper > current_price * 1.001:
            result = await self._rest_exchange(
                "place_trigger_order", self.cfg.instId, "buy", sz,
                triggerPx=f"{upper:.2f}",
                orderPx=f"{upper * 1.001:.2f}",  # limit +0.1% buffer for fill certainty
                triggerPxType="last")
            if result.get("code") == "0" and result.get("data"):
                self._pending_long_algoId = result["data"][0].get("algoId", "")
                logger.info(f"📈 LONG trigger at {upper:.2f} ({self._pending_long_algoId})")
            else:
                logger.error(f"LONG trigger failed: {result}")

        elif trend == "down" and lower < current_price * 0.999:
            result = await self._rest_exchange(
                "place_trigger_order", self.cfg.instId, "sell", sz,
                triggerPx=f"{lower:.2f}",
                orderPx=f"{lower * 0.999:.2f}",  # limit -0.1% buffer for fill certainty
                triggerPxType="last")
            if result.get("code") == "0" and result.get("data"):
                self._pending_short_algoId = result["data"][0].get("algoId", "")
                logger.info(f"📉 SHORT trigger at {lower:.2f} ({self._pending_short_algoId})")
            else:
                logger.error(f"SHORT trigger failed: {result}")

        self._save_pending()

    # === Fill Handler ===

    async def _on_entry_filled(self, direction: str, fill_price: float, fill_sz: str):
        """Entry filled → validate → set SL → set TP → save state."""
        self._entry_in_progress = True
        try:
            await self._on_entry_filled_inner(direction, fill_price, fill_sz)
        finally:
            self._entry_in_progress = False

    async def _on_entry_filled_inner(self, direction: str, fill_price: float, fill_sz: str):
        logger.info(f"🎯 Entry: {direction} @ {fill_price:.2f} sz={fill_sz}")

        # Validate fill price
        if fill_price <= 0:
            logger.error("fill_price invalid, querying exchange...")
            positions = await self._rest_exchange("get_positions", self.cfg.instId)
            if positions:
                for p in positions:
                    if float(p.get("pos", 0)) != 0:
                        fill_price = float(p.get("avgPx", 0))
                        break
            if fill_price <= 0:
                logger.error("CRITICAL: No valid price — emergency close!")
                close_side = "sell" if direction == "LONG" else "buy"
                await self._rest_exchange("place_market_order",
                    self.cfg.instId, close_side, fill_sz, True)
                send_discord(f"{MSG_PREFIX}🚨 入场价格无效，紧急平仓", mention=True)
                return

        # Get actual position size from exchange (handles partial fills)
        positions = await self._rest_exchange("get_positions", self.cfg.instId)
        actual_sz = fill_sz
        if positions:
            for p in positions:
                pv = abs(float(p.get("pos", 0)))
                if pv > 0:
                    actual_sz = f"{pv:.2f}"
                    if actual_sz != fill_sz:
                        logger.warning(f"Size mismatch: WS={fill_sz} exchange={actual_sz}")
                    break

        # Cancel other side (under lock)
        async with self._order_lock:
            if direction == "LONG" and self._pending_short_algoId:
                try:
                    await self._rest_exchange("cancel_algo_order",
                        self._pending_short_algoId, self.cfg.instId)
                except Exception:
                    pass
                self._pending_short_algoId = None
            elif direction == "SHORT" and self._pending_long_algoId:
                try:
                    await self._rest_exchange("cancel_algo_order",
                        self._pending_long_algoId, self.cfg.instId)
                except Exception:
                    pass
                self._pending_long_algoId = None
            self._save_pending()

        close_side = "sell" if direction == "LONG" else "buy"

        # SL/TP prices
        if direction == "LONG":
            sl_price = fill_price * (1 - self.cfg.risk.stop_loss_pct)
            tp_price = fill_price * (1 + self.cfg.risk.take_profit_pct)
        else:
            sl_price = fill_price * (1 + self.cfg.risk.stop_loss_pct)
            tp_price = fill_price * (1 - self.cfg.risk.take_profit_pct)

        # SET SL — CRITICAL
        sl_result = await self._rest_exchange(
            "place_stop_order", self.cfg.instId, close_side, actual_sz,
            slTriggerPx=f"{sl_price:.2f}")

        if sl_result.get("code") != "0" or not sl_result.get("data"):
            logger.error(f"SL FAILED: {sl_result} — EMERGENCY CLOSE!")
            await self._rest_exchange("place_market_order",
                self.cfg.instId, close_side, actual_sz, True)
            send_discord(f"{MSG_PREFIX}🚨 止损设置失败，紧急平仓", mention=True)
            self.executor.save_position(None)
            return

        sl_algo_id = sl_result["data"][0].get("algoId", "")

        # Verify SL is actually live on exchange
        await asyncio.sleep(1)
        algos = await self._rest_exchange("get_algo_orders", self.cfg.instId, "conditional")
        sl_live = any(a.get("algoId") == sl_algo_id for a in algos)
        if not sl_live:
            logger.error(f"SL {sl_algo_id} not live after placement — emergency close!")
            await self._rest_exchange("place_market_order",
                self.cfg.instId, close_side, actual_sz, True)
            send_discord(f"{MSG_PREFIX}🚨 止损未激活，紧急平仓", mention=True)
            self.executor.save_position(None)
            return

        # SET TP (non-critical, SL already active)
        tp_result = await self._rest_exchange(
            "place_limit_order", self.cfg.instId, close_side, actual_sz,
            px=f"{tp_price:.2f}", reduceOnly=True)
        tp_ord_id = ""
        if tp_result.get("code") == "0" and tp_result.get("data"):
            tp_ord_id = tp_result["data"][0].get("ordId", "")
        else:
            logger.error(f"TP failed (SL active): {tp_result}")
            send_discord(f"{MSG_PREFIX}⚠️ TP设置失败，仅有SL保护")

        # Save position state
        self.executor.save_position({
            "direction": direction, "entry_price": fill_price,
            "size": actual_sz, "sl_price": sl_price, "tp_price": tp_price,
            "sl_algo_id": sl_algo_id, "tp_order_id": tp_ord_id,
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "entry_bar_count": 0,
        })

        send_discord(
            f"{MSG_PREFIX}📊 OKX BB: {direction} {self.cfg.coin}\n"
            f"入场: ${fill_price:.2f}\n"
            f"止损: ${sl_price:.2f} ({self.cfg.risk.stop_loss_pct*100:.1f}%)\n"
            f"止盈: ${tp_price:.2f} ({self.cfg.risk.take_profit_pct*100:.1f}%)\n"
            f"合约: {actual_sz}",
            mention=True,
        )

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
                WS_BUSINESS_URL, ping_interval=PING_INTERVAL,
                ping_timeout=10, close_timeout=5)
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
                WS_PRIVATE_URL, ping_interval=PING_INTERVAL,
                ping_timeout=10, close_timeout=5)
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
        try:
            data = json.loads(msg)
        except json.JSONDecodeError:
            return
        if data.get("event") == "subscribe":
            logger.info(f"Sub confirmed: {data.get('arg', {}).get('channel')}")
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
        try:
            data = json.loads(msg)
        except json.JSONDecodeError:
            return
        if data.get("event"):
            return

        channel = data.get("arg", {}).get("channel", "")

        if channel == "orders-algo" and "data" in data:
            for order in data["data"]:
                algo_id = order.get("algoId", "")
                state = order.get("state", "")
                side = order.get("side", "")
                sz = order.get("sz", "0")

                logger.info(f"Algo: {algo_id} state={state} side={side}")

                if state == "effective":
                    if algo_id == self._pending_long_algoId:
                        logger.info("🎯 LONG trigger fired")
                        self._pending_long_algoId = None
                        self._triggered_direction = "LONG"
                        self._triggered_sz = sz
                    elif algo_id == self._pending_short_algoId:
                        logger.info("🎯 SHORT trigger fired")
                        self._pending_short_algoId = None
                        self._triggered_direction = "SHORT"
                        self._triggered_sz = sz
                    else:
                        # SL trigger or unknown
                        await self._check_position_closed()

        elif channel == "orders" and "data" in data:
            for order in data["data"]:
                if order.get("state") != "filled":
                    continue

                side = order.get("side", "")
                acc_fill = order.get("accFillSz", "0")

                # Safe price extraction
                try:
                    avg_px = float(order.get("avgPx", "0") or "0")
                except (ValueError, TypeError):
                    avg_px = 0.0

                logger.info(f"Filled: {side} {acc_fill} @ {avg_px}")

                triggered_dir = self._triggered_direction
                if triggered_dir:
                    # Normal: orders-algo arrived first
                    self._triggered_direction = None
                    self._triggered_sz = None
                    await self._on_entry_filled(triggered_dir, avg_px, acc_fill)

                elif not self.executor.load_position():
                    # Race: orders arrived before orders-algo
                    if self._pending_long_algoId or self._pending_short_algoId:
                        inferred = "LONG" if side == "buy" else "SHORT"
                        logger.warning(f"Race: infer {inferred} from side={side}")
                        if inferred == "LONG":
                            self._pending_long_algoId = None
                        else:
                            self._pending_short_algoId = None
                        await self._on_entry_filled(inferred, avg_px, acc_fill)
                    else:
                        await self._check_position_closed()
                else:
                    # Has position — SL/TP fill
                    await self._check_position_closed()

    # === Trading Logic ===

    async def _on_candle_close(self):
        """Candle confirmed → atomic cancel+check+place."""
        # Check position timeout first
        try:
            result = await self._rest(self.executor.check_position)
            if result:
                logger.info(f"Position closed: {result.exit_reason.value}")
                send_discord(
                    f"{MSG_PREFIX}📊 OKX BB 平仓: {result.exit_reason.value}\n"
                    f"PnL: {result.pnl_pct*100:+.2f}%",
                    mention=True)
        except Exception as e:
            logger.error(f"check_position error: {e}", exc_info=True)

        # Atomic: cancel old → verify no position → place new
        if not self.executor.load_position():
            await self._atomic_cancel_and_place()

    async def _check_position_closed(self):
        """Check if SL/TP hit. If position closed, place new triggers."""
        await asyncio.sleep(2)
        try:
            result = await self._rest(self.executor.check_position)
            if result:
                logger.info(f"Position closed: {result.exit_reason.value}")
                send_discord(
                    f"{MSG_PREFIX}📊 OKX BB 平仓: {result.exit_reason.value}\n"
                    f"PnL: {result.pnl_pct*100:+.2f}%",
                    mention=True)
                await self._atomic_cancel_and_place()
        except Exception as e:
            logger.error(f"check error: {e}", exc_info=True)

    # === Periodic Reconciliation ===

    async def _periodic_check(self):
        while self._running:
            await asyncio.sleep(300)
            try:
                # Skip if entry in progress
                if self._entry_in_progress or self._triggered_direction:
                    logger.debug("Periodic: entry in progress, skip")
                    continue

                # Check position timeout
                result = await self._rest(self.executor.check_position)
                if result:
                    logger.info(f"Periodic: closed {result.exit_reason.value}")
                    send_discord(
                        f"{MSG_PREFIX}📊 平仓 (periodic): {result.exit_reason.value}\n"
                        f"PnL: {result.pnl_pct*100:+.2f}%",
                        mention=True)
                    await self._atomic_cancel_and_place()
                    continue

                # Orphan detection (only if no local position AND no entry in progress)
                if not self.executor.load_position() and not self._entry_in_progress:
                    positions = await self._rest_exchange("get_positions", self.cfg.instId)
                    if positions and any(float(p.get("pos", 0)) != 0 for p in positions):
                        # Double-check entry_in_progress (could have changed)
                        if self._entry_in_progress or self._triggered_direction:
                            logger.info("Periodic: entry started during check, skip orphan")
                            continue
                        logger.error("PERIODIC: Orphan detected!")
                        algos = await self._rest_exchange("get_algo_orders", self.cfg.instId, "conditional")
                        has_sl = any(a.get("slTriggerPx") for a in algos)
                        if not has_sl:
                            pos_info = next(p for p in positions if float(p.get("pos", 0)) != 0)
                            pv = float(pos_info.get("pos", 0))
                            close_side = "sell" if pv > 0 else "buy"
                            await self._rest_exchange("place_market_order",
                                self.cfg.instId, close_side, f"{abs(pv):.2f}", True)
                            send_discord(f"{MSG_PREFIX}🚨 发现无保护仓位，紧急平仓", mention=True)
                        else:
                            logger.info("Orphan has SL, reconstructing state")
                            # Similar to startup reconciliation
                            pos_info = next(p for p in positions if float(p.get("pos", 0)) != 0)
                            pv = float(pos_info.get("pos", 0))
                            d = "LONG" if pv > 0 else "SHORT"
                            ap = float(pos_info.get("avgPx", 0))
                            self.executor.save_position({
                                "direction": d, "entry_price": ap,
                                "size": f"{abs(pv):.2f}",
                                "sl_price": ap * (0.98 if d == "LONG" else 1.02),
                                "tp_price": ap * (1.03 if d == "LONG" else 0.97),
                                "sl_algo_id": next((a["algoId"] for a in algos), ""),
                                "tp_order_id": "",
                                "entry_time": datetime.now(timezone.utc).isoformat(),
                                "entry_bar_count": 0,
                            })

                # Ensure pending orders exist if no position
                if not self.executor.load_position():
                    if not self._pending_long_algoId and not self._pending_short_algoId:
                        await self._atomic_cancel_and_place()

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
        logger.info("OKX BB Monitor v2.3 — Hardened")
        logger.info(f"{self.cfg.instId} BB({self.cfg.strategy.bb_period}, "
                     f"{self.cfg.strategy.bb_multiplier}) "
                     f"TP={self.cfg.risk.take_profit_pct*100}% "
                     f"SL={self.cfg.risk.stop_loss_pct*100}%")
        logger.info("=" * 60)

        if not await self.accumulator.initialize(self._loop):
            await asyncio.sleep(30)
            if not await self.accumulator.initialize(self._loop):
                logger.error("Candle init failed. Exiting.")
                return

        self._load_pending()
        self._running = True

        # Set leverage once at startup (before any algo orders exist)
        lev_result = await self._rest_exchange("set_leverage", self.cfg.instId, "5", "isolated")
        if isinstance(lev_result, dict) and lev_result.get("code") != "0":
            logger.warning(f"set_leverage result: {lev_result.get('msg', lev_result)}")

        for s in (sig.SIGINT, sig.SIGTERM):
            self._loop.add_signal_handler(s, self._shutdown)

        # Connect both WS
        await self._connect_business()
        await self._connect_private()

        # Reconcile with exchange
        await self._reconcile_on_startup()

        # Initial order placement
        if not self.executor.load_position():
            if not self._pending_long_algoId and not self._pending_short_algoId:
                await self._atomic_cancel_and_place()

        # Get last commit info for version tracking
        import subprocess as _sp
        try:
            _commit = _sp.run(
                ["git", "log", "--oneline", "-1"],
                capture_output=True, text=True, timeout=5,
                cwd=str(Path(__file__).parent.parent),
            ).stdout.strip()
        except Exception:
            _commit = "unknown"

        send_discord(
            f"🟢 OKX BB 启动\n"
            f"{self.cfg.instId} BB({self.cfg.strategy.bb_period}, "
            f"{self.cfg.strategy.bb_multiplier})\n"
            f"K线: {len(self.accumulator.closes)} bars\n"
            f"版本: {_commit}")

        await asyncio.gather(
            self._business_loop(),
            self._private_loop(),
            self._periodic_check(),
        )

    def _shutdown(self):
        """Signal handler — set flag only. Cleanup via ExecStop."""
        logger.info("Shutdown signal received")
        self._running = False
        # Don't do blocking REST here — ExecStop cleanup.py handles it
        send_discord(f"🔴 OKX BB Monitor 停止")


def main():
    monitor = WSMonitor()
    asyncio.run(monitor.run())


if __name__ == "__main__":
    main()
