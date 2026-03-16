"""Regression tests for 5 critical bugs found in code review (2026-03-16).

Bug 1: SOL orphan detection didn't verify SL → unprotected positions
Bug 2: Market order retry didn't handle delayed position detection
Bug 3: ticker price=0 triggered false SL close
Bug 4: fill_price=0 emergency close used WS fill_sz instead of exchange size
Bug 5: ETH _close_confirm_entry deadlock (lock re-entry)
"""
import sys
import asyncio
import time
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch
from datetime import datetime, timezone

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


async def _noop_sleep(delay):
    """Replacement for asyncio.sleep that doesn't actually sleep."""
    return

from okx_sol_bb.config import OKXSolConfig, StrategyConfig, RiskConfig, FeeConfig
from okx_bb.config import OKXConfig, StrategyConfig as EthStrategyConfig, RiskConfig as EthRiskConfig, FeeConfig as EthFeeConfig, ExecutionConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def make_sol_config():
    return OKXSolConfig(
        strategy=StrategyConfig(bb_period=14, bb_multiplier=3.0),
        risk=RiskConfig(stop_loss_pct=0.05, take_profit_pct=0.02, leverage=5),
        fees=FeeConfig(),
        api_key="test", secret_key="test", passphrase="test",
        coin="SOL", instId="SOL-USDT-SWAP",
    )


def make_sol_monitor():
    from okx_sol_bb.ws_monitor import WSMonitor
    m = WSMonitor(config=make_sol_config())
    m._loop = asyncio.new_event_loop()
    m.executor = MagicMock()
    m.executor.save_position = MagicMock()
    m.executor.load_position = MagicMock(return_value=None)
    m.executor.check_position = MagicMock(return_value=None)
    m.executor._append_open_trade_log_if_missing = MagicMock()
    m.executor.reconcile_position_from_exchange = MagicMock()
    m.executor.calculate_size = MagicMock(return_value="1.00")
    m._rest_exchange = AsyncMock()
    m.accumulator._initialized = True
    m.accumulator.closes = [150.0 + i * 0.1 for i in range(50)]
    return m


def make_eth_config():
    return OKXConfig(
        strategy=EthStrategyConfig(bb_period=20, bb_multiplier=2.5,
                                   trend_ema_period=96, trend_lookback=8),
        risk=EthRiskConfig(stop_loss_pct=0.02, take_profit_pct=0.03),
        fees=EthFeeConfig(),
        api_key="test", secret_key="test", passphrase="test",
        coin="ETH", instId="ETH-USDT-SWAP",
        execution=ExecutionConfig(mode="close_confirm_buffer"),
    )


def make_eth_monitor():
    from okx_bb.ws_monitor import WSMonitor
    m = WSMonitor(config=make_eth_config())
    m._loop = asyncio.new_event_loop()
    m.executor = MagicMock()
    m.executor.save_position = MagicMock()
    m.executor.load_position = MagicMock(return_value=None)
    m.executor.check_position = MagicMock(return_value=None)
    m.executor.reconcile_position_from_exchange = MagicMock()
    m.executor.calculate_size = MagicMock(return_value="0.10")
    m._pending_long_algoId = None
    m._pending_short_algoId = None
    m._entry_in_progress = False
    m._triggered_direction = None
    m._triggered_sz = None
    m._triggered_at = None
    m.accumulator._initialized = True
    m.accumulator.closes = [2000.0 + i * 0.5 for i in range(300)]
    return m


# ===========================================================================
# Bug 1: SOL orphan detection must verify SL — no SL → emergency close
# ===========================================================================

class TestBug1_OrphanNoSL:
    """Orphan detected on exchange with no SL algo → must emergency close."""

    def test_orphan_no_sl_places_market_close(self):
        """Exchange has position, local has none, no SL algo → place_market_order."""
        m = make_sol_monitor()
        m._last_activity = time.time()
        m.executor.load_position.return_value = None
        m.executor.check_position.return_value = None

        calls = {}

        async def mock_rest(method, *a, **kw):
            calls.setdefault(method, []).append((a, kw))
            if method == "get_positions":
                return [{"pos": "2.00", "avgPx": "150.0"}]
            if method == "get_algo_orders":
                return []  # NO SL!
            if method == "place_stop_order":
                # SL placement fails
                return {"code": "1", "data": []}
            if method == "place_market_order":
                return {"code": "0", "data": [{"ordId": "em_close"}]}
            return {"code": "0", "data": []}

        async def run():
            m._rest_exchange = mock_rest
            m._rest = AsyncMock(return_value=None)
            m._running = True

            async def stop_after_one(delay):
                m._running = False

            with patch("asyncio.sleep", stop_after_one):
                await m._periodic_check()

        _run(run())

        # Orphan with failed SL → emergency market close
        assert "place_market_order" in calls
        m.executor.save_position.assert_called_with(None)

    def test_orphan_no_sl_but_sl_reset_succeeds(self):
        """Orphan with no SL, but SL re-set succeeds → position saved with SL."""
        m = make_sol_monitor()
        m._last_activity = time.time()
        m.executor.load_position.return_value = None
        m.executor.check_position.return_value = None

        calls = {}

        async def mock_rest(method, *a, **kw):
            calls.setdefault(method, []).append((a, kw))
            if method == "get_positions":
                return [{"pos": "1.50", "avgPx": "145.0"}]
            if method == "get_algo_orders":
                return []  # No SL
            if method == "place_stop_order":
                return {"code": "0", "data": [{"algoId": "new_sl_123"}]}
            if method == "place_limit_order":
                return {"code": "0", "data": [{"ordId": "new_tp_456"}]}
            return {"code": "0", "data": []}

        async def run():
            m._rest_exchange = mock_rest
            m._rest = AsyncMock(return_value=None)
            m._running = True

            async def stop_after_one(delay):
                m._running = False

            with patch("asyncio.sleep", stop_after_one):
                await m._periodic_check()

        _run(run())

        # SL was placed, position saved
        assert "place_stop_order" in calls
        assert "place_market_order" not in calls
        m.executor.save_position.assert_called_once()
        pos = m.executor.save_position.call_args[0][0]
        assert pos is not None
        assert pos["sl_algo_id"] == "new_sl_123"

    def test_orphan_with_sl_just_reconciles(self):
        """Orphan with existing SL → reconcile only, no emergency close."""
        m = make_sol_monitor()
        m._last_activity = time.time()
        m.executor.load_position.return_value = None
        m.executor.check_position.return_value = None

        calls = {}

        async def mock_rest(method, *a, **kw):
            calls.setdefault(method, []).append((a, kw))
            if method == "get_positions":
                return [{"pos": "1.00", "avgPx": "150.0"}]
            if method == "get_algo_orders":
                return [{"algoId": "existing_sl", "slTriggerPx": "142.50"}]
            return {"code": "0", "data": []}

        async def run():
            m._rest_exchange = mock_rest
            m._rest = AsyncMock(return_value=None)
            m._running = True

            async def stop_after_one(delay):
                m._running = False

            with patch("asyncio.sleep", stop_after_one):
                await m._periodic_check()

        _run(run())

        # Has SL → just reconcile, no market close
        assert "place_market_order" not in calls
        m.executor.reconcile_position_from_exchange.assert_called_once_with(
            source="periodic_orphan")


# ===========================================================================
# Bug 2: Market order retry — position shows up on 3rd check
# ===========================================================================

class TestBug2_RetryPositionDetection:
    """_close_confirm_entry: market order succeeds but position only visible
    on 3rd get_positions call."""

    def test_sol_retry_finds_position_on_third_attempt(self):
        """SOL: first 2 get_positions empty, 3rd returns position → _on_entry_filled."""
        m = make_sol_monitor()
        m.executor.load_position.return_value = None

        get_pos_count = [0]
        on_entry_filled_calls = []

        async def mock_rest_ex(method, *a, **kw):
            if method == "get_positions":
                get_pos_count[0] += 1
                # Phase 1 check (verify no position before order): empty
                if get_pos_count[0] == 1:
                    return []
                # Phase 2 retry: first 2 empty, 3rd has position
                if get_pos_count[0] <= 3:
                    return []
                return [{"pos": "1.00", "avgPx": "148.50"}]
            if method == "place_market_order":
                return {"code": "0", "data": [{"ordId": "mkt_retry"}]}
            if method == "place_stop_order":
                return {"code": "0", "data": [{"algoId": "sl_retry"}]}
            if method == "get_algo_orders":
                return [{"algoId": "sl_retry", "slTriggerPx": "141.0"}]
            if method == "place_limit_order":
                return {"code": "0", "data": [{"ordId": "tp_retry"}]}
            return {"code": "0", "data": []}

        original_on_entry_filled = None

        async def track_on_entry_filled(direction, fill_price, fill_sz):
            on_entry_filled_calls.append((direction, fill_price, fill_sz))
            await original_on_entry_filled(direction, fill_price, fill_sz)

        async def run():
            nonlocal original_on_entry_filled
            m._rest_exchange = mock_rest_ex
            m._rest = AsyncMock(side_effect=lambda fn, *a, **kw: fn(*a, **kw) if callable(fn) else None)
            original_on_entry_filled = m._on_entry_filled
            m._on_entry_filled = track_on_entry_filled

            with patch("okx_sol_bb.ws_monitor.detect_signal", return_value="LONG"), \
                 patch("okx_sol_bb.ws_monitor.get_bb_levels", return_value=(150.0, 155.0, 145.0)), \
                 patch("asyncio.sleep", _noop_sleep):
                await m._close_confirm_entry()

        _run(run())

        assert len(on_entry_filled_calls) == 1
        assert on_entry_filled_calls[0][0] == "LONG"

    def test_eth_retry_finds_position_on_third_attempt(self):
        """ETH: same scenario — position detected on 3rd retry."""
        m = make_eth_monitor()
        m.executor.load_position.return_value = None

        get_pos_count = [0]
        on_entry_filled_calls = []

        async def mock_rest_ex(method, *a, **kw):
            if method == "get_positions":
                get_pos_count[0] += 1
                if get_pos_count[0] == 1:
                    return []
                if get_pos_count[0] <= 3:
                    return []
                return [{"pos": "0.10", "avgPx": "2050.0"}]
            if method == "place_market_order":
                return {"code": "0", "data": [{"ordId": "mkt_eth"}]}
            if method == "place_stop_order":
                return {"code": "0", "data": [{"algoId": "sl_eth"}]}
            if method == "get_algo_orders":
                return [{"algoId": "sl_eth", "slTriggerPx": "2009.0"}]
            if method == "place_limit_order":
                return {"code": "0", "data": [{"ordId": "tp_eth"}]}
            if method == "cancel_algo_order":
                return {"code": "0"}
            return {"code": "0", "data": []}

        original_on_entry_filled = None

        async def track_on_entry_filled(direction, fill_price, fill_sz):
            on_entry_filled_calls.append((direction, fill_price, fill_sz))
            await original_on_entry_filled(direction, fill_price, fill_sz)

        async def run():
            nonlocal original_on_entry_filled
            m._rest_exchange = mock_rest_ex
            m._rest = AsyncMock(side_effect=lambda fn, *a, **kw: fn(*a, **kw) if callable(fn) else None)
            original_on_entry_filled = m._on_entry_filled
            m._on_entry_filled = track_on_entry_filled

            with patch("okx_bb.ws_monitor.get_bb_levels", return_value=(2000, 2050, 1950)), \
                 patch.object(m, "_get_trend", return_value="up"), \
                 patch("asyncio.sleep", _noop_sleep):
                await m._close_confirm_entry()

        _run(run())

        assert len(on_entry_filled_calls) == 1
        assert on_entry_filled_calls[0][0] == "LONG"


# ===========================================================================
# Bug 3: ticker price=0 must NOT trigger false SL close
# ===========================================================================

class TestBug3_TickerPriceZero:
    """Periodic check: when ticker returns price=0, should NOT market close."""

    def test_sol_ticker_zero_no_market_close(self):
        """SOL: SL missing, ticker.last=0 → skip SL breach check, re-set SL."""
        m = make_sol_monitor()
        m._last_activity = time.time()

        local_pos = {
            "direction": "LONG", "entry_price": 150.0,
            "size": "1.00", "sl_price": 142.5,
            "sl_algo_id": "old_sl",
        }
        m.executor.load_position.return_value = local_pos
        m.executor.check_position.return_value = None

        calls = {}

        async def mock_rest(method, *a, **kw):
            calls.setdefault(method, []).append((a, kw))
            if method == "get_algo_orders":
                return []  # No SL
            if method == "get_positions":
                return [{"pos": "1.00", "avgPx": "150.0"}]
            if method == "get_ticker":
                return {"last": 0}  # INVALID price
            if method == "place_stop_order":
                return {"code": "0", "data": [{"algoId": "new_sl"}]}
            if method == "place_market_order":
                return {"code": "0", "data": [{"ordId": "bad_close"}]}
            return {"code": "0", "data": []}

        async def run():
            m._rest_exchange = mock_rest
            m._rest = AsyncMock(return_value=None)
            m._running = True

            async def stop_after_one(delay):
                m._running = False

            with patch("asyncio.sleep", stop_after_one):
                await m._periodic_check()

        _run(run())

        # Must NOT have called place_market_order due to price=0
        assert "place_market_order" not in calls
        # Should have re-set SL instead
        assert "place_stop_order" in calls

    def test_eth_ticker_zero_no_market_close(self):
        """ETH: same — ticker price=0 → no false close."""
        m = make_eth_monitor()
        m._last_activity = time.time()

        local_pos = {
            "direction": "LONG", "entry_price": 2000.0,
            "size": "0.10", "sl_price": 1960.0,
            "sl_algo_id": "old_sl",
        }
        m.executor.load_position.return_value = local_pos
        m.executor.check_position.return_value = None

        calls = {}

        async def mock_rest(method, *a, **kw):
            calls.setdefault(method, []).append((a, kw))
            if method == "get_algo_orders":
                return []
            if method == "get_positions":
                return [{"pos": "0.10", "avgPx": "2000.0"}]
            if method == "get_ticker":
                return {"last": 0}
            if method == "place_stop_order":
                return {"code": "0", "data": [{"algoId": "new_sl_eth"}]}
            if method == "place_market_order":
                return {"code": "0", "data": [{"ordId": "bad"}]}
            return {"code": "0", "data": []}

        async def run():
            m._rest_exchange = mock_rest
            m._rest = AsyncMock(return_value=None)
            m._running = True

            async def stop_after_one(delay):
                m._running = False

            with patch("asyncio.sleep", stop_after_one):
                await m._periodic_check()

        _run(run())

        assert "place_market_order" not in calls
        assert "place_stop_order" in calls

    def test_sol_ticker_valid_below_sl_does_close(self):
        """Control: valid ticker price below SL → DOES market close."""
        m = make_sol_monitor()
        m._last_activity = time.time()

        local_pos = {
            "direction": "LONG", "entry_price": 150.0,
            "size": "1.00", "sl_price": 142.5,
            "sl_algo_id": "old_sl",
        }
        m.executor.load_position.return_value = local_pos
        m.executor.check_position.return_value = None

        calls = {}

        async def mock_rest(method, *a, **kw):
            calls.setdefault(method, []).append((a, kw))
            if method == "get_algo_orders":
                return []
            if method == "get_positions":
                return [{"pos": "1.00", "avgPx": "150.0"}]
            if method == "get_ticker":
                return {"last": 140.0}  # Below SL!
            if method == "place_market_order":
                return {"code": "0", "data": [{"ordId": "close_ok"}]}
            return {"code": "0", "data": []}

        async def run():
            m._rest_exchange = mock_rest
            m._rest = AsyncMock(return_value=None)
            m._running = True

            async def stop_after_one(delay):
                m._running = False

            with patch("asyncio.sleep", stop_after_one):
                await m._periodic_check()

        _run(run())

        assert "place_market_order" in calls
        m.executor.save_position.assert_called_with(None)


# ===========================================================================
# Bug 4: fill_price=0 emergency close must use exchange size, not WS fill_sz
# ===========================================================================

class TestBug4_FillPriceZeroUsesExchangeSize:
    """When fill_price=0 and exchange avgPx also 0, emergency close
    must use exchange pos size, not the WS-reported fill_sz."""

    def test_emergency_close_uses_exchange_size(self):
        """fill_sz="3.00" but exchange pos=5.00 → close with "5.00"."""
        m = make_sol_monitor()

        calls = {}

        async def mock_rest(method, *a, **kw):
            calls.setdefault(method, []).append((a, kw))
            if method == "get_positions":
                return [{"pos": "5.00", "avgPx": "0"}]  # avgPx also 0!
            if method == "place_market_order":
                return {"code": "0", "data": [{"ordId": "em_close"}]}
            return {"code": "0", "data": []}

        async def run():
            m._rest_exchange = mock_rest
            # fill_price=0, fill_sz="3.00" (stale WS data)
            await m._on_entry_filled("LONG", 0.0, "3.00")

        _run(run())

        # Emergency close should use exchange size "5.00", not WS "3.00"
        assert "place_market_order" in calls
        mkt_call = calls["place_market_order"][0]
        # Args: (instId, close_side, close_sz, reduceOnly)
        close_sz = mkt_call[0][2]
        assert close_sz == "5.00", f"Expected exchange size '5.00', got '{close_sz}'"

    def test_emergency_close_falls_back_to_fill_sz_when_no_exchange_pos(self):
        """No exchange position → uses fill_sz as fallback."""
        m = make_sol_monitor()

        calls = {}

        async def mock_rest(method, *a, **kw):
            calls.setdefault(method, []).append((a, kw))
            if method == "get_positions":
                return []  # No position on exchange (already closed?)
            if method == "place_market_order":
                return {"code": "0", "data": [{"ordId": "em_close2"}]}
            return {"code": "0", "data": []}

        async def run():
            m._rest_exchange = mock_rest
            await m._on_entry_filled("LONG", 0.0, "3.00")

        _run(run())

        assert "place_market_order" in calls
        close_sz = calls["place_market_order"][0][0][2]
        assert close_sz == "3.00"

    def test_valid_fill_price_uses_exchange_size_for_sl(self):
        """Even with valid fill_price, actual_sz comes from exchange."""
        m = make_sol_monitor()

        calls = {}

        async def mock_rest(method, *a, **kw):
            calls.setdefault(method, []).append((a, kw))
            if method == "get_positions":
                return [{"pos": "4.50", "avgPx": "150.0"}]
            if method == "place_stop_order":
                return {"code": "0", "data": [{"algoId": "sl_x"}]}
            if method == "get_algo_orders":
                return [{"algoId": "sl_x", "slTriggerPx": "142.50"}]
            if method == "place_limit_order":
                return {"code": "0", "data": [{"ordId": "tp_x"}]}
            return {"code": "0", "data": []}

        async def run():
            m._rest_exchange = mock_rest
            await m._on_entry_filled("LONG", 150.0, "3.00")  # WS says 3.00

        _run(run())

        pos = m.executor.save_position.call_args[0][0]
        # Size should be from exchange "4.50", not WS "3.00"
        assert pos["size"] == "4.50"


# ===========================================================================
# Bug 5: ETH _close_confirm_entry deadlock (lock re-entry)
# ===========================================================================

class TestBug5_EthDeadlock:
    """_close_confirm_entry releases _order_lock before calling _on_entry_filled,
    which acquires _order_lock internally. If lock isn't released → deadlock."""

    def test_close_confirm_entry_completes_without_deadlock(self):
        """Full flow: signal → market order → position detected → _on_entry_filled
        → SL/TP set → all within 5 seconds (no deadlock)."""
        m = make_eth_monitor()
        m.executor.load_position.return_value = None

        get_pos_count = [0]

        async def mock_rest_ex(method, *a, **kw):
            if method == "get_positions":
                get_pos_count[0] += 1
                if get_pos_count[0] <= 1:
                    return []  # Pre-order check
                return [{"pos": "0.10", "avgPx": "2050.0"}]  # Post-order
            if method == "place_market_order":
                return {"code": "0", "data": [{"ordId": "mkt_deadlock"}]}
            if method == "place_stop_order":
                return {"code": "0", "data": [{"algoId": "sl_deadlock"}]}
            if method == "get_algo_orders":
                return [{"algoId": "sl_deadlock", "slTriggerPx": "2009.0"}]
            if method == "place_limit_order":
                return {"code": "0", "data": [{"ordId": "tp_deadlock"}]}
            if method == "cancel_algo_order":
                return {"code": "0"}
            return {"code": "0", "data": []}

        async def run():
            m._rest_exchange = mock_rest_ex
            m._rest = AsyncMock(side_effect=lambda fn, *a, **kw: fn(*a, **kw) if callable(fn) else None)

            with patch("okx_bb.ws_monitor.get_bb_levels", return_value=(2000, 2060, 1940)), \
                 patch.object(m, "_get_trend", return_value="up"), \
                 patch("asyncio.sleep", _noop_sleep):
                # Timeout = 5s. If deadlocked, this will raise TimeoutError.
                await asyncio.wait_for(m._close_confirm_entry(), timeout=5.0)

        _run(run())

        # Verify full flow completed: position saved with SL/TP
        m.executor.save_position.assert_called_once()
        pos = m.executor.save_position.call_args[0][0]
        assert pos is not None
        assert pos["direction"] == "LONG"
        assert pos["sl_algo_id"] == "sl_deadlock"
        assert pos["tp_order_id"] is not None

    def test_lock_not_held_during_on_entry_filled(self):
        """Verify _order_lock is NOT held when _on_entry_filled runs."""
        m = make_eth_monitor()
        m.executor.load_position.return_value = None

        lock_held_during_fill = [None]
        get_pos_count = [0]

        original_on_entry_filled = m._on_entry_filled

        async def spy_on_entry_filled(direction, fill_price, fill_sz):
            lock_held_during_fill[0] = m._order_lock.locked()
            await original_on_entry_filled(direction, fill_price, fill_sz)

        async def mock_rest_ex(method, *a, **kw):
            if method == "get_positions":
                get_pos_count[0] += 1
                if get_pos_count[0] <= 1:
                    return []
                return [{"pos": "0.10", "avgPx": "2050.0"}]
            if method == "place_market_order":
                return {"code": "0", "data": [{"ordId": "mkt_lock"}]}
            if method == "place_stop_order":
                return {"code": "0", "data": [{"algoId": "sl_lock"}]}
            if method == "get_algo_orders":
                return [{"algoId": "sl_lock", "slTriggerPx": "2009.0"}]
            if method == "place_limit_order":
                return {"code": "0", "data": [{"ordId": "tp_lock"}]}
            if method == "cancel_algo_order":
                return {"code": "0"}
            return {"code": "0", "data": []}

        async def run():
            m._rest_exchange = mock_rest_ex
            m._rest = AsyncMock(side_effect=lambda fn, *a, **kw: fn(*a, **kw) if callable(fn) else None)
            m._on_entry_filled = spy_on_entry_filled

            with patch("okx_bb.ws_monitor.get_bb_levels", return_value=(2000, 2060, 1940)), \
                 patch.object(m, "_get_trend", return_value="up"), \
                 patch("asyncio.sleep", _noop_sleep):
                await asyncio.wait_for(m._close_confirm_entry(), timeout=5.0)

        _run(run())

        # _order_lock should NOT be held when _on_entry_filled is called
        # (Phase 2 runs outside the lock)
        assert lock_held_during_fill[0] is False, \
            "_order_lock was still held during _on_entry_filled — deadlock risk!"

    def test_sol_close_confirm_entry_no_deadlock(self):
        """SOL version: same deadlock test."""
        m = make_sol_monitor()
        m.executor.load_position.return_value = None

        get_pos_count = [0]

        async def mock_rest_ex(method, *a, **kw):
            if method == "get_positions":
                get_pos_count[0] += 1
                if get_pos_count[0] <= 1:
                    return []
                return [{"pos": "1.00", "avgPx": "149.0"}]
            if method == "place_market_order":
                return {"code": "0", "data": [{"ordId": "mkt_sol_dl"}]}
            if method == "place_stop_order":
                return {"code": "0", "data": [{"algoId": "sl_sol_dl"}]}
            if method == "get_algo_orders":
                return [{"algoId": "sl_sol_dl", "slTriggerPx": "141.55"}]
            if method == "place_limit_order":
                return {"code": "0", "data": [{"ordId": "tp_sol_dl"}]}
            return {"code": "0", "data": []}

        async def run():
            m._rest_exchange = mock_rest_ex
            m._rest = AsyncMock(side_effect=lambda fn, *a, **kw: fn(*a, **kw) if callable(fn) else None)

            with patch("okx_sol_bb.ws_monitor.detect_signal", return_value="LONG"), \
                 patch("okx_sol_bb.ws_monitor.get_bb_levels", return_value=(150, 155, 145)), \
                 patch("asyncio.sleep", _noop_sleep):
                await asyncio.wait_for(m._close_confirm_entry(), timeout=5.0)

        _run(run())

        m.executor.save_position.assert_called_once()
        pos = m.executor.save_position.call_args[0][0]
        assert pos is not None
        assert pos["sl_algo_id"] == "sl_sol_dl"
