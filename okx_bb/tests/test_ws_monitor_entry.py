"""Tests for WSMonitor entry/exit critical path: _on_entry_filled, _check_position_closed.

These test the core SL/TP placement flow and position close detection.
"""
import sys
import asyncio
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch, call
from datetime import datetime, timezone

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from okx_bb.config import OKXConfig, StrategyConfig, RiskConfig, FeeConfig, ExecutionConfig


def make_config(sl_pct=0.03, tp_pct=0.04, mode="close_confirm_buffer"):
    return OKXConfig(
        strategy=StrategyConfig(),
        risk=RiskConfig(stop_loss_pct=sl_pct, take_profit_pct=tp_pct),
        fees=FeeConfig(),
        api_key="test", secret_key="test", passphrase="test",
        coin="ETH", instId="ETH-USDT-SWAP",
        execution=ExecutionConfig(mode=mode),
    )


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def make_monitor(sl_pct=0.03, tp_pct=0.04):
    from okx_bb.ws_monitor import WSMonitor
    cfg = make_config(sl_pct, tp_pct)
    m = WSMonitor(config=cfg)
    m._loop = asyncio.new_event_loop()
    m.executor = MagicMock()
    m._pending_long_algoId = None
    m._pending_short_algoId = None
    m._entry_in_progress = False
    m._triggered_direction = None
    m._triggered_sz = None
    return m


class TestOnEntryFilledInner:
    """Tests for _on_entry_filled_inner — the core SL/TP placement flow."""

    def test_long_entry_sets_sl_and_tp(self):
        """Normal LONG entry: SL below, TP above, state saved."""
        m = make_monitor(sl_pct=0.03, tp_pct=0.04)

        calls = {}

        async def mock_rest_exchange(method, *args, **kwargs):
            calls.setdefault(method, []).append((args, kwargs))
            if method == "get_positions":
                return [{"pos": "0.41", "avgPx": "2000.0"}]
            elif method == "place_stop_order":
                return {"code": "0", "data": [{"algoId": "sl_123"}]}
            elif method == "get_algo_orders":
                return [{"algoId": "sl_123", "slTriggerPx": "1940.0"}]
            elif method == "place_limit_order":
                return {"code": "0", "data": [{"ordId": "tp_456"}]}
            elif method == "cancel_algo_order":
                return {"code": "0"}
            return {}

        m._rest_exchange = mock_rest_exchange
        m._save_pending = MagicMock()

        _run(m._on_entry_filled_inner("LONG", 2000.0, "0.41"))

        # SL placed
        assert "place_stop_order" in calls
        sl_args = calls["place_stop_order"][0]
        assert "1940.00" in str(sl_args)  # 2000 * (1 - 0.03)

        # TP placed
        assert "place_limit_order" in calls

        # State saved
        m.executor.save_position.assert_called_once()
        saved = m.executor.save_position.call_args[0][0]
        assert saved["direction"] == "LONG"
        assert saved["entry_price"] == 2000.0
        assert saved["sl_algo_id"] == "sl_123"
        assert saved["tp_order_id"] == "tp_456"

    def test_short_entry_sets_sl_above_and_tp_below(self):
        """SHORT entry: SL above entry, TP below."""
        m = make_monitor(sl_pct=0.03, tp_pct=0.04)

        calls = {}

        async def mock_rest_exchange(method, *args, **kwargs):
            calls.setdefault(method, []).append((args, kwargs))
            if method == "get_positions":
                return [{"pos": "-0.41", "avgPx": "2000.0"}]
            elif method == "place_stop_order":
                return {"code": "0", "data": [{"algoId": "sl_s123"}]}
            elif method == "get_algo_orders":
                return [{"algoId": "sl_s123", "slTriggerPx": "2060.0"}]
            elif method == "place_limit_order":
                return {"code": "0", "data": [{"ordId": "tp_s456"}]}
            return {}

        m._rest_exchange = mock_rest_exchange
        m._save_pending = MagicMock()

        _run(m._on_entry_filled_inner("SHORT", 2000.0, "0.41"))

        saved = m.executor.save_position.call_args[0][0]
        assert saved["direction"] == "SHORT"
        assert saved["sl_price"] == 2000.0 * 1.03  # SL above for SHORT
        assert saved["tp_price"] == 2000.0 * 0.96  # TP below for SHORT

    def test_sl_failure_triggers_emergency_close(self):
        """If SL placement fails → emergency market close."""
        m = make_monitor()

        async def mock_rest_exchange(method, *args, **kwargs):
            if method == "get_positions":
                return [{"pos": "0.41", "avgPx": "2000.0"}]
            elif method == "place_stop_order":
                return {"code": "1", "data": [{"sCode": "51280", "sMsg": "error"}]}
            elif method == "place_market_order":
                return {"code": "0"}
            return {}

        m._rest_exchange = mock_rest_exchange
        m._save_pending = MagicMock()

        _run(m._on_entry_filled_inner("LONG", 2000.0, "0.41"))

        # Position cleared (emergency close)
        m.executor.save_position.assert_called_with(None)

    def test_sl_not_live_triggers_emergency_close(self):
        """SL placed but not live on exchange → emergency close."""
        m = make_monitor()

        async def mock_rest_exchange(method, *args, **kwargs):
            if method == "get_positions":
                return [{"pos": "0.41", "avgPx": "2000.0"}]
            elif method == "place_stop_order":
                return {"code": "0", "data": [{"algoId": "sl_ghost"}]}
            elif method == "get_algo_orders":
                return []  # SL not found on exchange!
            elif method == "place_market_order":
                return {"code": "0"}
            return {}

        m._rest_exchange = mock_rest_exchange
        m._save_pending = MagicMock()

        _run(m._on_entry_filled_inner("LONG", 2000.0, "0.41"))

        m.executor.save_position.assert_called_with(None)

    def test_tp_failure_still_saves_position(self):
        """TP fails but SL is active → position saved (SL protection)."""
        m = make_monitor()

        async def mock_rest_exchange(method, *args, **kwargs):
            if method == "get_positions":
                return [{"pos": "0.41", "avgPx": "2000.0"}]
            elif method == "place_stop_order":
                return {"code": "0", "data": [{"algoId": "sl_ok"}]}
            elif method == "get_algo_orders":
                return [{"algoId": "sl_ok", "slTriggerPx": "1940"}]
            elif method == "place_limit_order":
                return {"code": "1", "data": [{"sMsg": "TP failed"}]}
            return {}

        m._rest_exchange = mock_rest_exchange
        m._save_pending = MagicMock()

        _run(m._on_entry_filled_inner("LONG", 2000.0, "0.41"))

        # Still saved with SL (TP is non-critical)
        saved = m.executor.save_position.call_args[0][0]
        assert saved["sl_algo_id"] == "sl_ok"
        assert saved["tp_order_id"] == ""  # TP failed

    def test_zero_fill_price_queries_exchange(self):
        """fill_price=0 → query exchange for avgPx."""
        m = make_monitor()

        async def mock_rest_exchange(method, *args, **kwargs):
            if method == "get_positions":
                return [{"pos": "0.41", "avgPx": "2050.0"}]
            elif method == "place_stop_order":
                return {"code": "0", "data": [{"algoId": "sl_x"}]}
            elif method == "get_algo_orders":
                return [{"algoId": "sl_x", "slTriggerPx": "1988.5"}]
            elif method == "place_limit_order":
                return {"code": "0", "data": [{"ordId": "tp_x"}]}
            return {}

        m._rest_exchange = mock_rest_exchange
        m._save_pending = MagicMock()

        _run(m._on_entry_filled_inner("LONG", 0.0, "0.41"))

        saved = m.executor.save_position.call_args[0][0]
        assert saved["entry_price"] == 2050.0  # Got from exchange

    def test_zero_fill_price_no_exchange_data_emergency_close(self):
        """fill_price=0 and no exchange data → emergency close."""
        m = make_monitor()

        close_called = []

        async def mock_rest_exchange(method, *args, **kwargs):
            if method == "get_positions":
                return []  # No positions
            elif method == "place_market_order":
                close_called.append(args)
                return {"code": "0"}
            return {}

        m._rest_exchange = mock_rest_exchange
        m._save_pending = MagicMock()

        _run(m._on_entry_filled_inner("LONG", 0.0, "0.41"))

        # Should NOT save position (emergency close path)
        # The function returns early after emergency close
        assert len(close_called) > 0

    def test_exchange_size_overrides_ws_size(self):
        """If exchange shows different size from WS fill, use exchange size."""
        m = make_monitor()

        async def mock_rest_exchange(method, *args, **kwargs):
            if method == "get_positions":
                return [{"pos": "0.40", "avgPx": "2000.0"}]  # Exchange: 0.40
            elif method == "place_stop_order":
                return {"code": "0", "data": [{"algoId": "sl_sz"}]}
            elif method == "get_algo_orders":
                return [{"algoId": "sl_sz", "slTriggerPx": "1940"}]
            elif method == "place_limit_order":
                return {"code": "0", "data": [{"ordId": "tp_sz"}]}
            return {}

        m._rest_exchange = mock_rest_exchange
        m._save_pending = MagicMock()

        _run(m._on_entry_filled_inner("LONG", 2000.0, "0.41"))  # WS: 0.41

        saved = m.executor.save_position.call_args[0][0]
        assert saved["size"] == "0.40"  # Exchange size wins

    def test_cancels_opposite_trigger_on_long_fill(self):
        """LONG fill → cancel pending SHORT trigger."""
        m = make_monitor()
        m._pending_short_algoId = "short_trigger_123"
        m._pending_long_algoId = None

        cancelled = []

        async def mock_rest_exchange(method, *args, **kwargs):
            if method == "cancel_algo_order":
                cancelled.append(args[0])
                return {"code": "0"}
            elif method == "get_positions":
                return [{"pos": "0.41", "avgPx": "2000.0"}]
            elif method == "place_stop_order":
                return {"code": "0", "data": [{"algoId": "sl_c"}]}
            elif method == "get_algo_orders":
                return [{"algoId": "sl_c", "slTriggerPx": "1940"}]
            elif method == "place_limit_order":
                return {"code": "0", "data": [{"ordId": "tp_c"}]}
            return {}

        m._rest_exchange = mock_rest_exchange
        m._save_pending = MagicMock()

        _run(m._on_entry_filled_inner("LONG", 2000.0, "0.41"))

        assert "short_trigger_123" in cancelled
        assert m._pending_short_algoId is None


class TestOnEntryFilledException:
    """Tests for _on_entry_filled exception handling wrapper."""

    def test_exception_with_no_sl_triggers_emergency_close(self):
        """Exception during entry + no SL on exchange → emergency close."""
        m = make_monitor()

        async def mock_inner(*args, **kwargs):
            raise RuntimeError("test error")

        m._on_entry_filled_inner = mock_inner

        close_called = []

        async def mock_rest_exchange(method, *args, **kwargs):
            if method == "get_positions":
                return [{"pos": "0.41", "avgPx": "2000.0"}]
            elif method == "get_algo_orders":
                return []  # No SL
            elif method == "place_market_order":
                close_called.append(True)
                return {"code": "0"}
            return {}

        m._rest_exchange = mock_rest_exchange

        _run(m._on_entry_filled("LONG", 2000.0, "0.41"))

        assert len(close_called) > 0
        assert m._entry_in_progress is False  # Reset in finally

    def test_exception_with_existing_sl_keeps_position(self):
        """Exception during entry but SL exists → position is safe."""
        m = make_monitor()

        async def mock_inner(*args, **kwargs):
            raise RuntimeError("test error")

        m._on_entry_filled_inner = mock_inner

        async def mock_rest_exchange(method, *args, **kwargs):
            if method == "get_positions":
                return [{"pos": "0.41", "avgPx": "2000.0"}]
            elif method == "get_algo_orders":
                return [{"algoId": "sl_safe", "slTriggerPx": "1940"}]
            return {}

        m._rest_exchange = mock_rest_exchange

        _run(m._on_entry_filled("LONG", 2000.0, "0.41"))

        # save_position(None) should NOT be called — SL exists
        m.executor.save_position.assert_not_called()
        assert m._entry_in_progress is False

    def test_entry_in_progress_flag(self):
        """_entry_in_progress is True during fill, False after."""
        m = make_monitor()
        flags = []

        async def mock_inner(*args, **kwargs):
            flags.append(m._entry_in_progress)

        m._on_entry_filled_inner = mock_inner

        _run(m._on_entry_filled("LONG", 2000.0, "0.41"))

        assert flags[0] is True  # Was True during execution
        assert m._entry_in_progress is False  # Reset after


class TestCheckPositionClosed:
    """Tests for _check_position_closed — detecting SL/TP fills."""

    def test_position_closed_notifies(self):
        """executor.check_position returns result → position closed."""
        m = make_monitor()
        from types import SimpleNamespace
        from enum import Enum

        class ExitReason(Enum):
            SL = "SL"

        class Direction(Enum):
            LONG = "LONG"

        result = SimpleNamespace(
            exit_reason=ExitReason.SL, direction=Direction.LONG,
            coin="ETH", entry_price=2000.0, exit_price=1940.0, pnl_pct=-0.03
        )

        async def mock_rest(fn, *args, **kwargs):
            return fn()

        m._rest = mock_rest
        m.executor.check_position.return_value = result

        _run(m._check_position_closed())

        # check_position was called (via _rest)
        m.executor.check_position.assert_called()

    def test_no_close_no_action(self):
        """executor.check_position returns None → no action."""
        m = make_monitor()

        async def mock_rest(fn, *args, **kwargs):
            return fn()

        m._rest = mock_rest
        m.executor.check_position.return_value = None

        _run(m._check_position_closed())

    def test_exception_handled(self):
        """Exception in check_position → logged, no crash."""
        m = make_monitor()

        async def mock_rest(fn, *args, **kwargs):
            raise RuntimeError("API error")

        m._rest = mock_rest

        # Should not raise
        _run(m._check_position_closed())


class TestWSMessageRouting:
    """Tests for _handle_private_message — orders/orders-algo routing."""

    def _msg(self, data):
        """Convert dict to JSON string (ws_monitor expects string)."""
        import json
        return json.dumps(data)

    def test_algo_effective_triggers_long(self):
        """orders-algo: state=effective, matching long algoId → trigger LONG."""
        m = make_monitor()
        m._pending_long_algoId = "algo_long_1"
        m._pending_short_algoId = None

        msg = self._msg({
            "arg": {"channel": "orders-algo"},
            "data": [{
                "algoId": "algo_long_1",
                "state": "effective",
                "side": "buy",
                "sz": "0.41"
            }]
        })

        _run(m._handle_private_message(msg))

        assert m._triggered_direction == "LONG"
        assert m._pending_long_algoId is None

    def test_algo_effective_triggers_short(self):
        """orders-algo: state=effective, matching short algoId → trigger SHORT."""
        m = make_monitor()
        m._pending_long_algoId = None
        m._pending_short_algoId = "algo_short_1"

        msg = self._msg({
            "arg": {"channel": "orders-algo"},
            "data": [{
                "algoId": "algo_short_1",
                "state": "effective",
                "side": "sell",
                "sz": "0.41"
            }]
        })

        _run(m._handle_private_message(msg))

        assert m._triggered_direction == "SHORT"
        assert m._pending_short_algoId is None

    def test_algo_unknown_id_checks_position_closed(self):
        """orders-algo: effective but unknown algoId → check position closed (SL trigger)."""
        m = make_monitor()
        m._pending_long_algoId = "different_id"
        m._pending_short_algoId = None
        m._check_position_closed = AsyncMock()

        msg = self._msg({
            "arg": {"channel": "orders-algo"},
            "data": [{
                "algoId": "unknown_algo",
                "state": "effective",
                "side": "sell",
                "sz": "0.41"
            }]
        })

        _run(m._handle_private_message(msg))

        m._check_position_closed.assert_called_once()

    def test_order_filled_with_triggered_direction(self):
        """orders: filled + triggered_direction set → call _on_entry_filled."""
        m = make_monitor()
        m._triggered_direction = "LONG"
        m._triggered_sz = "0.41"
        m._on_entry_filled = AsyncMock()

        msg = self._msg({
            "arg": {"channel": "orders"},
            "data": [{
                "state": "filled",
                "side": "buy",
                "accFillSz": "0.41",
                "avgPx": "2050.0"
            }]
        })

        _run(m._handle_private_message(msg))

        m._on_entry_filled.assert_called_once_with("LONG", 2050.0, "0.41")
        assert m._triggered_direction is None

    def test_order_filled_with_position_checks_close(self):
        """orders: filled + has position (no triggered dir) → check position closed."""
        m = make_monitor()
        m._triggered_direction = None
        m.executor.load_position.return_value = {"direction": "LONG"}
        m._check_position_closed = AsyncMock()

        msg = self._msg({
            "arg": {"channel": "orders"},
            "data": [{
                "state": "filled",
                "side": "sell",
                "accFillSz": "0.41",
                "avgPx": "2050.0"
            }]
        })

        _run(m._handle_private_message(msg))

        m._check_position_closed.assert_called_once()

    def test_order_filled_race_condition_infers_direction(self):
        """orders: filled before orders-algo + pending trigger → infer direction."""
        m = make_monitor()
        m._triggered_direction = None
        m._pending_long_algoId = "pending_123"
        m._pending_short_algoId = None
        m.executor.load_position.return_value = None  # No position yet
        m._on_entry_filled = AsyncMock()

        msg = self._msg({
            "arg": {"channel": "orders"},
            "data": [{
                "state": "filled",
                "side": "buy",
                "accFillSz": "0.41",
                "avgPx": "2050.0"
            }]
        })

        _run(m._handle_private_message(msg))

        m._on_entry_filled.assert_called_once_with("LONG", 2050.0, "0.41")
        assert m._pending_long_algoId is None

    def test_order_not_filled_ignored(self):
        """orders: state != filled → ignored."""
        m = make_monitor()
        m._on_entry_filled = AsyncMock()
        m._check_position_closed = AsyncMock()

        msg = self._msg({
            "arg": {"channel": "orders"},
            "data": [{
                "state": "partially_filled",
                "side": "buy",
                "accFillSz": "0.20",
                "avgPx": "2050.0"
            }]
        })

        _run(m._handle_private_message(msg))

        m._on_entry_filled.assert_not_called()
        m._check_position_closed.assert_not_called()

    def test_invalid_json_ignored(self):
        """Invalid JSON → silently ignored."""
        m = make_monitor()
        m._on_entry_filled = AsyncMock()

        _run(m._handle_private_message("not json {{{"))

        m._on_entry_filled.assert_not_called()

    def test_event_message_ignored(self):
        """Messages with 'event' key → ignored."""
        m = make_monitor()
        m._on_entry_filled = AsyncMock()

        msg = self._msg({"event": "subscribe", "arg": {"channel": "orders"}})
        _run(m._handle_private_message(msg))

        m._on_entry_filled.assert_not_called()
