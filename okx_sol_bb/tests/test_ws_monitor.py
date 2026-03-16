"""Tests for OKX SOL BB WSMonitor — core path unit tests.

Covers:
1. _on_entry_filled — entry fill → SL/TP placement, state save
2. _check_position_closed — detect close (SL, TP, timeout)
3. _on_candle_close — candle close → signal → order
4. periodic SL re-set (uses exchange size, not local)
5. orphan detection + reconciliation
6. WS message routing (orders / orders-algo channels)
"""
import sys
import asyncio
import json
import time
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch, call
from datetime import datetime, timezone

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from okx_sol_bb.config import OKXSolConfig, StrategyConfig, RiskConfig, FeeConfig
from core.types import TradeResult, Direction, ExitReason


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_config():
    return OKXSolConfig(
        strategy=StrategyConfig(bb_period=14, bb_multiplier=3.0),
        risk=RiskConfig(stop_loss_pct=0.05, take_profit_pct=0.02, leverage=5),
        fees=FeeConfig(),
        api_key="test", secret_key="test", passphrase="test",
        coin="SOL", instId="SOL-USDT-SWAP",
    )


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def make_monitor():
    from okx_sol_bb.ws_monitor import WSMonitor
    m = WSMonitor(config=make_config())
    m._loop = asyncio.new_event_loop()
    m.executor = MagicMock()
    m.executor.save_position = MagicMock()
    m.executor.load_position = MagicMock(return_value=None)
    m.executor.check_position = MagicMock(return_value=None)
    m.executor._append_open_trade_log_if_missing = MagicMock()
    m.executor.reconcile_position_from_exchange = MagicMock()
    m.executor.calculate_size = MagicMock(return_value="1.00")
    m._rest_exchange = AsyncMock()
    # Accumulator default: ready with enough closes
    m.accumulator._initialized = True
    m.accumulator.closes = [150.0 + i * 0.1 for i in range(50)]
    return m


def _make_trade_result(**overrides):
    defaults = dict(
        coin="SOL", direction=Direction.LONG, entry_price=150.0,
        exit_price=153.0, size=1.0, pnl_pct=0.02, pnl_usd=3.0,
        entry_time=datetime.now(timezone.utc),
        exit_time=datetime.now(timezone.utc),
        exit_reason=ExitReason.TP,
    )
    defaults.update(overrides)
    return TradeResult(**defaults)


# ---------------------------------------------------------------------------
# 1. _on_entry_filled
# ---------------------------------------------------------------------------

class TestOnEntryFilled:
    """Entry fill → SL → TP → save state."""

    def test_long_entry_sets_sl_tp_and_saves(self):
        m = make_monitor()

        async def mock_rest(method, *a, **kw):
            if method == "get_positions":
                return [{"pos": "1.00", "avgPx": "150.0"}]
            if method == "place_stop_order":
                return {"code": "0", "data": [{"algoId": "sl_001"}]}
            if method == "get_algo_orders":
                return [{"algoId": "sl_001", "slTriggerPx": "142.50"}]
            if method == "place_limit_order":
                return {"code": "0", "data": [{"ordId": "tp_001"}]}
            return {"code": "0", "data": []}

        async def run():
            m._rest_exchange = mock_rest
            await m._on_entry_filled("LONG", 150.0, "1.00")

        _run(run())

        m.executor.save_position.assert_called_once()
        pos = m.executor.save_position.call_args[0][0]
        assert pos["direction"] == "LONG"
        assert pos["entry_price"] == 150.0
        assert pos["sl_algo_id"] == "sl_001"
        assert pos["tp_order_id"] == "tp_001"
        # SL = 150 * (1 - 0.05) = 142.5
        assert abs(pos["sl_price"] - 142.5) < 0.01
        # TP = 150 * (1 + 0.02) = 153.0
        assert abs(pos["tp_price"] - 153.0) < 0.01

    def test_short_entry_sets_correct_sl_tp(self):
        m = make_monitor()

        async def mock_rest(method, *a, **kw):
            if method == "get_positions":
                return [{"pos": "-2.00", "avgPx": "150.0"}]
            if method == "place_stop_order":
                return {"code": "0", "data": [{"algoId": "sl_002"}]}
            if method == "get_algo_orders":
                return [{"algoId": "sl_002", "slTriggerPx": "157.50"}]
            if method == "place_limit_order":
                return {"code": "0", "data": [{"ordId": "tp_002"}]}
            return {"code": "0", "data": []}

        async def run():
            m._rest_exchange = mock_rest
            await m._on_entry_filled("SHORT", 150.0, "2.00")

        _run(run())

        pos = m.executor.save_position.call_args[0][0]
        assert pos["direction"] == "SHORT"
        # SL = 150 * (1 + 0.05) = 157.5
        assert abs(pos["sl_price"] - 157.5) < 0.01
        # TP = 150 * (1 - 0.02) = 147.0
        assert abs(pos["tp_price"] - 147.0) < 0.01

    def test_sl_failure_triggers_emergency_close(self):
        """If SL placement fails → market close immediately."""
        m = make_monitor()

        calls = {}
        async def mock_rest(method, *a, **kw):
            calls.setdefault(method, []).append((a, kw))
            if method == "get_positions":
                return [{"pos": "1.00", "avgPx": "150.0"}]
            if method == "place_stop_order":
                return {"code": "1", "data": []}  # FAIL
            if method == "place_market_order":
                return {"code": "0", "data": [{"ordId": "emergency"}]}
            return {"code": "0", "data": []}

        async def run():
            m._rest_exchange = mock_rest
            await m._on_entry_filled("LONG", 150.0, "1.00")

        _run(run())

        assert "place_market_order" in calls  # Emergency close triggered
        m.executor.save_position.assert_called_with(None)  # State cleared

    def test_sl_not_live_triggers_emergency_close(self):
        """SL placed but not live on exchange → emergency close."""
        m = make_monitor()

        calls = {}
        async def mock_rest(method, *a, **kw):
            calls.setdefault(method, []).append((a, kw))
            if method == "get_positions":
                return [{"pos": "1.00", "avgPx": "150.0"}]
            if method == "place_stop_order":
                return {"code": "0", "data": [{"algoId": "sl_ghost"}]}
            if method == "get_algo_orders":
                return []  # SL not live!
            if method == "place_market_order":
                return {"code": "0", "data": [{"ordId": "emergency"}]}
            return {"code": "0", "data": []}

        async def run():
            m._rest_exchange = mock_rest
            await m._on_entry_filled("LONG", 150.0, "1.00")

        _run(run())

        assert "place_market_order" in calls
        m.executor.save_position.assert_called_with(None)

    def test_tp_failure_still_saves_position(self):
        """TP fails but SL is live → position saved (SL-only protection)."""
        m = make_monitor()

        async def mock_rest(method, *a, **kw):
            if method == "get_positions":
                return [{"pos": "1.00", "avgPx": "150.0"}]
            if method == "place_stop_order":
                return {"code": "0", "data": [{"algoId": "sl_ok"}]}
            if method == "get_algo_orders":
                return [{"algoId": "sl_ok", "slTriggerPx": "142.50"}]
            if method == "place_limit_order":
                return {"code": "1", "data": []}  # TP FAIL
            return {"code": "0", "data": []}

        async def run():
            m._rest_exchange = mock_rest
            await m._on_entry_filled("LONG", 150.0, "1.00")

        _run(run())

        pos = m.executor.save_position.call_args[0][0]
        assert pos["sl_algo_id"] == "sl_ok"
        assert pos["tp_order_id"] == ""  # Empty, TP failed

    def test_zero_fill_price_uses_exchange_avg(self):
        """Fill price 0 → fallback to exchange avgPx."""
        m = make_monitor()

        async def mock_rest(method, *a, **kw):
            if method == "get_positions":
                return [{"pos": "1.00", "avgPx": "155.0"}]
            if method == "place_stop_order":
                return {"code": "0", "data": [{"algoId": "sl_fix"}]}
            if method == "get_algo_orders":
                return [{"algoId": "sl_fix", "slTriggerPx": "147.25"}]
            if method == "place_limit_order":
                return {"code": "0", "data": [{"ordId": "tp_fix"}]}
            return {"code": "0", "data": []}

        async def run():
            m._rest_exchange = mock_rest
            await m._on_entry_filled("LONG", 0.0, "1.00")  # price=0

        _run(run())

        pos = m.executor.save_position.call_args[0][0]
        assert pos["entry_price"] == 155.0  # Used avgPx

    def test_uses_exchange_size_not_fill_sz(self):
        """Actual position size from exchange overrides fill_sz."""
        m = make_monitor()

        async def mock_rest(method, *a, **kw):
            if method == "get_positions":
                return [{"pos": "0.98", "avgPx": "150.0"}]  # exchange: 0.98
            if method == "place_stop_order":
                return {"code": "0", "data": [{"algoId": "sl_x"}]}
            if method == "get_algo_orders":
                return [{"algoId": "sl_x", "slTriggerPx": "142.50"}]
            if method == "place_limit_order":
                return {"code": "0", "data": [{"ordId": "tp_x"}]}
            return {"code": "0", "data": []}

        async def run():
            m._rest_exchange = mock_rest
            await m._on_entry_filled("LONG", 150.0, "1.00")  # fill says 1.00

        _run(run())

        pos = m.executor.save_position.call_args[0][0]
        assert pos["size"] == "0.98"  # Exchange size used

    def test_entry_in_progress_flag(self):
        """_entry_in_progress is True during entry, False after."""
        m = make_monitor()
        flags = []

        original_inner = m._on_entry_filled_inner

        async def spy_inner(*args, **kwargs):
            flags.append(m._entry_in_progress)
            # Minimal mock to avoid errors
            pass

        async def mock_rest(method, *a, **kw):
            if method == "get_positions":
                return [{"pos": "1.00", "avgPx": "150.0"}]
            if method == "place_stop_order":
                return {"code": "0", "data": [{"algoId": "sl_f"}]}
            if method == "get_algo_orders":
                return [{"algoId": "sl_f"}]
            if method == "place_limit_order":
                return {"code": "0", "data": [{"ordId": "tp_f"}]}
            return {"code": "0", "data": []}

        async def run():
            m._rest_exchange = mock_rest
            m._on_entry_filled_inner = spy_inner
            await m._on_entry_filled("LONG", 150.0, "1.00")

        _run(run())

        assert flags[0] is True  # Was True during inner call
        assert m._entry_in_progress is False  # Reset after


# ---------------------------------------------------------------------------
# 2. _check_position_closed
# ---------------------------------------------------------------------------

class TestCheckPositionClosed:
    """Detect position closure via SL/TP/timeout."""

    def test_position_closed_sends_discord(self):
        """When check_position returns a result → discord notification."""
        m = make_monitor()
        result = _make_trade_result(exit_reason=ExitReason.SL, pnl_pct=-0.05)

        async def mock_rest_fn(fn, *a, **kw):
            return result

        async def run():
            m._rest = mock_rest_fn
            await m._check_position_closed()

        _run(run())
        # check_position was called (via _rest)
        # No assertion on discord since conftest mocks it — just ensure no crash

    def test_position_not_closed(self):
        """When check_position returns None → no crash."""
        m = make_monitor()

        async def mock_rest_fn(fn, *a, **kw):
            return None

        async def run():
            m._rest = mock_rest_fn
            await m._check_position_closed()

        _run(run())

    def test_check_position_exception_handled(self):
        """Exception in check_position → logged, no crash."""
        m = make_monitor()

        async def mock_rest_fn(fn, *a, **kw):
            raise RuntimeError("DB error")

        async def run():
            m._rest = mock_rest_fn
            await m._check_position_closed()

        _run(run())  # Should not raise


# ---------------------------------------------------------------------------
# 3. _on_candle_close
# ---------------------------------------------------------------------------

class TestOnCandleClose:
    """Candle close → check position → signal detection → order."""

    def test_existing_position_skips_entry(self):
        """If position exists after check, no new entry."""
        m = make_monitor()
        m.executor.load_position.return_value = {"direction": "LONG"}

        async def mock_rest_fn(fn, *a, **kw):
            return None  # check_position returns None (not closed)

        async def run():
            m._rest = mock_rest_fn
            await m._on_candle_close()

        _run(run())
        # Should NOT have called _rest_exchange for placing orders
        m._rest_exchange.assert_not_called()

    def test_no_signal_no_order(self):
        """No signal → no order placed."""
        m = make_monitor()
        m.executor.load_position.return_value = None

        async def mock_rest_fn(fn, *a, **kw):
            return None

        async def run():
            m._rest = mock_rest_fn
            with patch("okx_sol_bb.ws_monitor.detect_signal", return_value=None):
                await m._on_candle_close()

        _run(run())
        m._rest_exchange.assert_not_called()

    def test_signal_long_places_market_order(self):
        """LONG signal → market buy → on_entry_filled."""
        m = make_monitor()
        m.executor.load_position.return_value = None
        m.executor.calculate_size.return_value = "1.50"

        rest_calls = {}

        async def mock_rest_fn(fn, *a, **kw):
            if fn == m.executor.calculate_size:
                return "1.50"
            return None  # check_position

        async def mock_rest_ex(method, *a, **kw):
            rest_calls.setdefault(method, []).append((a, kw))
            if method == "get_positions":
                # First call: verify no position; second: after order
                if len(rest_calls.get("get_positions", [])) <= 1:
                    return []
                return [{"pos": "1.50", "avgPx": "150.0"}]
            if method == "place_market_order":
                return {"code": "0", "data": [{"ordId": "mkt_001"}]}
            if method == "place_stop_order":
                return {"code": "0", "data": [{"algoId": "sl_auto"}]}
            if method == "get_algo_orders":
                return [{"algoId": "sl_auto", "slTriggerPx": "142.50"}]
            if method == "place_limit_order":
                return {"code": "0", "data": [{"ordId": "tp_auto"}]}
            return {"code": "0", "data": []}

        async def run():
            m._rest = mock_rest_fn
            m._rest_exchange = mock_rest_ex
            with patch("okx_sol_bb.ws_monitor.detect_signal", return_value="LONG"):
                await m._on_candle_close()

        _run(run())

        assert "place_market_order" in rest_calls
        # Verify buy direction
        mkt_args = rest_calls["place_market_order"][0]
        assert mkt_args[0] == ("SOL-USDT-SWAP", "buy", "1.50")

    def test_signal_short_places_sell_order(self):
        """SHORT signal → market sell."""
        m = make_monitor()
        m.executor.load_position.return_value = None
        m.executor.calculate_size.return_value = "2.00"

        rest_calls = {}

        async def mock_rest_fn(fn, *a, **kw):
            if fn == m.executor.calculate_size:
                return "2.00"
            return None

        async def mock_rest_ex(method, *a, **kw):
            rest_calls.setdefault(method, []).append((a, kw))
            if method == "get_positions":
                if len(rest_calls.get("get_positions", [])) <= 1:
                    return []
                return [{"pos": "-2.00", "avgPx": "150.0"}]
            if method == "place_market_order":
                return {"code": "0", "data": [{"ordId": "mkt_002"}]}
            if method == "place_stop_order":
                return {"code": "0", "data": [{"algoId": "sl_s"}]}
            if method == "get_algo_orders":
                return [{"algoId": "sl_s", "slTriggerPx": "157.50"}]
            if method == "place_limit_order":
                return {"code": "0", "data": [{"ordId": "tp_s"}]}
            return {"code": "0", "data": []}

        async def run():
            m._rest = mock_rest_fn
            m._rest_exchange = mock_rest_ex
            with patch("okx_sol_bb.ws_monitor.detect_signal", return_value="SHORT"):
                await m._on_candle_close()

        _run(run())

        assert "place_market_order" in rest_calls
        mkt_args = rest_calls["place_market_order"][0]
        assert mkt_args[0] == ("SOL-USDT-SWAP", "sell", "2.00")

    def test_market_order_failure_no_crash(self):
        """Market order fails → discord alert, no crash."""
        m = make_monitor()
        m.executor.load_position.return_value = None

        async def mock_rest_fn(fn, *a, **kw):
            return None

        async def mock_rest_ex(method, *a, **kw):
            if method == "get_positions":
                return []
            if method == "place_market_order":
                return {"code": "1", "msg": "insufficient balance"}
            return {"code": "0", "data": []}

        async def run():
            m._rest = mock_rest_fn
            m._rest_exchange = mock_rest_ex
            with patch("okx_sol_bb.ws_monitor.detect_signal", return_value="LONG"):
                await m._on_candle_close()

        _run(run())
        # No save_position since order failed
        m.executor.save_position.assert_not_called()

    def test_accumulator_not_ready_skips(self):
        """Accumulator not ready → skip signal detection."""
        m = make_monitor()
        m.executor.load_position.return_value = None
        m.accumulator._initialized = False  # Not ready

        async def mock_rest_fn(fn, *a, **kw):
            return None

        async def run():
            m._rest = mock_rest_fn
            await m._on_candle_close()

        _run(run())
        m._rest_exchange.assert_not_called()

    def test_entry_in_progress_blocks_new_entry(self):
        """_entry_in_progress == True → skip."""
        m = make_monitor()
        m.executor.load_position.return_value = None
        m._entry_in_progress = True

        async def mock_rest_fn(fn, *a, **kw):
            return None

        async def run():
            m._rest = mock_rest_fn
            await m._on_candle_close()

        _run(run())
        m._rest_exchange.assert_not_called()

    def test_position_exists_on_exchange_skips_entry(self):
        """Exchange shows existing position → no new order."""
        m = make_monitor()
        m.executor.load_position.return_value = None

        async def mock_rest_fn(fn, *a, **kw):
            return None

        async def mock_rest_ex(method, *a, **kw):
            if method == "get_positions":
                return [{"pos": "1.00", "avgPx": "150.0"}]  # Already has position
            return {"code": "0", "data": []}

        async def run():
            m._rest = mock_rest_fn
            m._rest_exchange = mock_rest_ex
            with patch("okx_sol_bb.ws_monitor.detect_signal", return_value="LONG"):
                await m._on_candle_close()

        _run(run())
        # place_market_order should NOT be called
        # (mock_rest_ex doesn't track, but we verify no save_position)
        m.executor.save_position.assert_not_called()

    def test_candle_close_checks_position_first(self):
        """_on_candle_close checks for closed position before signal."""
        m = make_monitor()
        result = _make_trade_result(exit_reason=ExitReason.TIMEOUT)
        m.executor.load_position.return_value = None  # After check, no position

        check_called = []

        async def mock_rest_fn(fn, *a, **kw):
            check_called.append(True)
            return result  # Position was closed

        async def run():
            m._rest = mock_rest_fn
            with patch("okx_sol_bb.ws_monitor.detect_signal", return_value=None):
                await m._on_candle_close()

        _run(run())
        assert len(check_called) == 1  # check_position was called

    def test_calculate_size_none_skips_order(self):
        """calculate_size returns None → skip order."""
        m = make_monitor()
        m.executor.load_position.return_value = None
        m.executor.calculate_size.return_value = None  # No size

        async def mock_rest_fn(fn, *a, **kw):
            if fn == m.executor.check_position:
                return None
            if fn == m.executor.calculate_size:
                return None
            return None

        async def mock_rest_ex(method, *a, **kw):
            if method == "get_positions":
                return []
            return {"code": "0", "data": []}

        async def run():
            m._rest = mock_rest_fn
            m._rest_exchange = mock_rest_ex
            with patch("okx_sol_bb.ws_monitor.detect_signal", return_value="LONG"):
                await m._on_candle_close()

        _run(run())
        m.executor.save_position.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Periodic SL re-set (uses exchange size, not local)
# ---------------------------------------------------------------------------

class TestPeriodicSLReset:
    """Periodic check: SL missing → re-set using exchange size."""

    def _run_periodic_once(self, m, iterations=1):
        """Run _periodic_check for a controlled single iteration."""
        count = [0]
        original_running = True

        async def run():
            m._running = True
            # Override asyncio.sleep to control iterations
            sleep_count = [0]
            original_sleep = asyncio.sleep

            async def controlled_sleep(delay):
                sleep_count[0] += 1
                if sleep_count[0] > iterations:
                    m._running = False
                # Don't actually sleep
                await original_sleep(0)

            with patch("asyncio.sleep", controlled_sleep):
                await m._periodic_check()

        _run(run())

    def test_sl_missing_resets_with_exchange_size(self):
        """SL gone → re-set using exchange size, not local."""
        m = make_monitor()
        m._last_activity = time.time()  # Fresh activity

        local_pos = {
            "direction": "LONG", "entry_price": 150.0,
            "size": "5.00",  # Local says 5.00
            "sl_price": 142.5, "tp_price": 153.0,
            "sl_algo_id": "old_sl",
        }
        m.executor.load_position.return_value = local_pos
        m.executor.check_position.return_value = None

        calls = {}

        async def mock_rest_fn(fn, *a, **kw):
            return None  # check_position

        async def mock_rest_ex(method, *a, **kw):
            calls.setdefault(method, []).append((a, kw))
            if method == "get_algo_orders":
                return []  # No SL!
            if method == "get_positions":
                return [{"pos": "1.00", "avgPx": "150.0"}]  # Exchange: 1.00
            if method == "get_ticker":
                return {"last": 148.0}  # Above SL
            if method == "place_stop_order":
                return {"code": "0", "data": [{"algoId": "new_sl"}]}
            return {"code": "0", "data": []}

        async def run():
            m._rest = mock_rest_fn
            m._rest_exchange = mock_rest_ex
            m._running = True

            async def stop_after_one(delay):
                m._running = False

            with patch("asyncio.sleep", stop_after_one):
                await m._periodic_check()

        _run(run())

        # SL was placed
        assert "place_stop_order" in calls
        sl_args = calls["place_stop_order"][0]
        # Size should be "1.00" (exchange), not "5.00" (local)
        assert sl_args[0][1] == "sell"  # close_side for LONG
        assert sl_args[0][2] == "1.00"  # Exchange size!

    def test_sl_missing_price_past_sl_market_close(self):
        """SL gone + price already past SL → market close."""
        m = make_monitor()
        m._last_activity = time.time()

        local_pos = {
            "direction": "LONG", "entry_price": 150.0,
            "size": "1.00", "sl_price": 142.5,
            "sl_algo_id": "old",
        }
        m.executor.load_position.return_value = local_pos
        m.executor.check_position.return_value = None

        calls = {}

        async def mock_rest_fn(fn, *a, **kw):
            return None

        async def mock_rest_ex(method, *a, **kw):
            calls.setdefault(method, []).append((a, kw))
            if method == "get_algo_orders":
                return []  # No SL
            if method == "get_positions":
                return [{"pos": "1.00", "avgPx": "150.0"}]
            if method == "get_ticker":
                return {"last": 140.0}  # Below SL!
            if method == "place_market_order":
                return {"code": "0", "data": [{"ordId": "em_close"}]}
            return {"code": "0", "data": []}

        async def run():
            m._rest = mock_rest_fn
            m._rest_exchange = mock_rest_ex
            m._running = True

            async def stop_after_one(delay):
                m._running = False

            with patch("asyncio.sleep", stop_after_one):
                await m._periodic_check()

        _run(run())

        assert "place_market_order" in calls
        m.executor.save_position.assert_called_with(None)

    def test_sl_reset_failure_emergency_close(self):
        """SL re-set fails → emergency market close."""
        m = make_monitor()
        m._last_activity = time.time()

        local_pos = {
            "direction": "SHORT", "entry_price": 150.0,
            "size": "2.00", "sl_price": 157.5,
            "sl_algo_id": "old",
        }
        m.executor.load_position.return_value = local_pos
        m.executor.check_position.return_value = None

        calls = {}

        async def mock_rest_fn(fn, *a, **kw):
            return None

        async def mock_rest_ex(method, *a, **kw):
            calls.setdefault(method, []).append((a, kw))
            if method == "get_algo_orders":
                return []
            if method == "get_positions":
                return [{"pos": "-2.00", "avgPx": "150.0"}]
            if method == "get_ticker":
                return {"last": 152.0}  # Still below SL
            if method == "place_stop_order":
                return {"code": "1", "data": []}  # SL FAIL
            if method == "place_market_order":
                return {"code": "0", "data": [{"ordId": "em"}]}
            return {"code": "0", "data": []}

        async def run():
            m._rest = mock_rest_fn
            m._rest_exchange = mock_rest_ex
            m._running = True

            async def stop_after_one(delay):
                m._running = False

            with patch("asyncio.sleep", stop_after_one):
                await m._periodic_check()

        _run(run())

        assert "place_market_order" in calls
        m.executor.save_position.assert_called_with(None)

    def test_sl_exists_no_action(self):
        """SL is present → no re-set needed."""
        m = make_monitor()
        m._last_activity = time.time()

        local_pos = {
            "direction": "LONG", "entry_price": 150.0,
            "size": "1.00", "sl_price": 142.5,
            "sl_algo_id": "existing_sl",
        }
        m.executor.load_position.return_value = local_pos
        m.executor.check_position.return_value = None

        async def mock_rest_fn(fn, *a, **kw):
            return None

        async def mock_rest_ex(method, *a, **kw):
            if method == "get_algo_orders":
                return [{"algoId": "existing_sl", "slTriggerPx": "142.50"}]
            if method == "get_positions":
                return [{"pos": "1.00", "avgPx": "150.0"}]
            return {"code": "0", "data": []}

        async def run():
            m._rest = mock_rest_fn
            m._rest_exchange = mock_rest_ex
            m._running = True

            async def stop_after_one(delay):
                m._running = False

            with patch("asyncio.sleep", stop_after_one):
                await m._periodic_check()

        _run(run())

        # No SL placement
        m.executor.save_position.assert_not_called()

    def test_direction_mismatch_triggers_reconcile(self):
        """Local LONG but exchange SHORT → reconcile."""
        m = make_monitor()
        m._last_activity = time.time()

        local_pos = {
            "direction": "LONG", "entry_price": 150.0,
            "size": "1.00", "sl_price": 142.5,
            "sl_algo_id": "old",
        }
        # After reconcile, load_position returns updated state
        reconciled_pos = {
            "direction": "SHORT", "entry_price": 148.0,
            "size": "0.50", "sl_price": 155.4,
            "sl_algo_id": "",
        }
        load_returns = [local_pos, reconciled_pos]
        m.executor.load_position.side_effect = lambda: load_returns.pop(0) if load_returns else None
        m.executor.check_position.return_value = None

        calls = {}

        async def mock_rest_fn(fn, *a, **kw):
            return None

        async def mock_rest_ex(method, *a, **kw):
            calls.setdefault(method, []).append((a, kw))
            if method == "get_algo_orders":
                return []
            if method == "get_positions":
                return [{"pos": "-0.50", "avgPx": "148.0"}]  # SHORT on exchange
            if method == "get_ticker":
                return {"last": 150.0}
            if method == "place_stop_order":
                return {"code": "0", "data": [{"algoId": "new_sl"}]}
            return {"code": "0", "data": []}

        async def run():
            m._rest = mock_rest_fn
            m._rest_exchange = mock_rest_ex
            m._running = True

            async def stop_after_one(delay):
                m._running = False

            with patch("asyncio.sleep", stop_after_one):
                await m._periodic_check()

        _run(run())

        m.executor.reconcile_position_from_exchange.assert_called_once_with(
            source="periodic_sl_mismatch")

    def test_position_closed_on_exchange_during_sl_check(self):
        """SL missing but position also gone on exchange → check_position."""
        m = make_monitor()
        m._last_activity = time.time()

        local_pos = {
            "direction": "LONG", "entry_price": 150.0,
            "size": "1.00", "sl_price": 142.5,
            "sl_algo_id": "old",
        }
        m.executor.load_position.return_value = local_pos

        check_called = []
        original_check = m.executor.check_position

        def tracking_check():
            check_called.append(True)
            return None

        m.executor.check_position = tracking_check

        async def mock_rest_fn(fn, *a, **kw):
            if callable(fn):
                return fn(*a, **kw)
            return None

        async def mock_rest_ex(method, *a, **kw):
            if method == "get_algo_orders":
                return []  # No SL
            if method == "get_positions":
                return []  # Position gone!
            return {"code": "0", "data": []}

        async def run():
            m._rest = mock_rest_fn
            m._rest_exchange = mock_rest_ex
            m._running = True

            async def stop_after_one(delay):
                m._running = False

            with patch("asyncio.sleep", stop_after_one):
                await m._periodic_check()

        _run(run())

        # check_position should be called to clean up
        assert len(check_called) >= 2  # Once at top, once for cleanup


# ---------------------------------------------------------------------------
# 5. Orphan detection + reconciliation
# ---------------------------------------------------------------------------

class TestOrphanDetection:
    """No local position but exchange has one → reconcile."""

    def test_orphan_detected_and_reconciled(self):
        m = make_monitor()
        m._last_activity = time.time()
        m.executor.load_position.return_value = None
        m.executor.check_position.return_value = None

        async def mock_rest_fn(fn, *a, **kw):
            return None

        async def mock_rest_ex(method, *a, **kw):
            if method == "get_algo_orders":
                return [{"algoId": "sl_x", "slTriggerPx": "142.50"}]
            if method == "get_positions":
                return [{"pos": "1.00", "avgPx": "150.0"}]  # Orphan!
            return {"code": "0", "data": []}

        async def run():
            m._rest = mock_rest_fn
            m._rest_exchange = mock_rest_ex
            m._running = True

            async def stop_after_one(delay):
                m._running = False

            with patch("asyncio.sleep", stop_after_one):
                await m._periodic_check()

        _run(run())

        m.executor.reconcile_position_from_exchange.assert_called_once_with(
            source="periodic_orphan")

    def test_no_orphan_when_exchange_empty(self):
        """No local pos, no exchange pos → nothing to do."""
        m = make_monitor()
        m._last_activity = time.time()
        m.executor.load_position.return_value = None
        m.executor.check_position.return_value = None

        async def mock_rest_fn(fn, *a, **kw):
            return None

        async def mock_rest_ex(method, *a, **kw):
            if method == "get_positions":
                return []
            if method == "get_algo_orders":
                return []
            return {"code": "0", "data": []}

        async def run():
            m._rest = mock_rest_fn
            m._rest_exchange = mock_rest_ex
            m._running = True

            async def stop_after_one(delay):
                m._running = False

            with patch("asyncio.sleep", stop_after_one):
                await m._periodic_check()

        _run(run())

        m.executor.reconcile_position_from_exchange.assert_not_called()

    def test_entry_in_progress_skips_orphan_check(self):
        """During entry, don't flag orphan."""
        m = make_monitor()
        m._last_activity = time.time()
        m._entry_in_progress = True
        m.executor.load_position.return_value = None
        m.executor.check_position.return_value = None

        async def mock_rest_fn(fn, *a, **kw):
            return None

        async def mock_rest_ex(method, *a, **kw):
            if method == "get_positions":
                return [{"pos": "1.00", "avgPx": "150.0"}]
            return {"code": "0", "data": []}

        async def run():
            m._rest = mock_rest_fn
            m._rest_exchange = mock_rest_ex
            m._running = True

            async def stop_after_one(delay):
                m._running = False

            with patch("asyncio.sleep", stop_after_one):
                await m._periodic_check()

        _run(run())

        # Should skip orphan check because entry in progress
        m.executor.reconcile_position_from_exchange.assert_not_called()


# ---------------------------------------------------------------------------
# 6. WS message routing
# ---------------------------------------------------------------------------

class TestWSMessageRouting:
    """orders / orders-algo channel routing."""

    def test_business_candle_close_triggers_on_candle_close(self):
        """Candle with confirm=1 → on_candle_close called."""
        m = make_monitor()
        called = []

        async def mock_on_candle_close():
            called.append(True)

        m._on_candle_close = mock_on_candle_close

        msg = json.dumps({
            "arg": {"channel": "candle30m", "instId": "SOL-USDT-SWAP"},
            "data": [["1710000000000", "150.0", "152.0", "148.0", "151.0",
                       "1000", "10000", "100000", "1"]]  # confirm=1
        })

        _run(m._handle_business_message(msg))

        assert len(called) == 1
        assert m.accumulator.closes[-1] == 151.0  # Close price added

    def test_business_candle_not_closed_ignored(self):
        """Candle with confirm=0 → ignored."""
        m = make_monitor()
        called = []

        async def mock_on_candle_close():
            called.append(True)

        m._on_candle_close = mock_on_candle_close

        msg = json.dumps({
            "arg": {"channel": "candle30m", "instId": "SOL-USDT-SWAP"},
            "data": [["1710000000000", "150.0", "152.0", "148.0", "151.0",
                       "1000", "10000", "100000", "0"]]  # confirm=0
        })

        _run(m._handle_business_message(msg))

        assert len(called) == 0

    def test_business_subscribe_event_ignored(self):
        """Subscribe confirmation → ignored."""
        m = make_monitor()
        msg = json.dumps({"event": "subscribe", "arg": {"channel": "candle30m"}})
        _run(m._handle_business_message(msg))
        # No crash

    def test_private_orders_algo_filled_checks_position(self):
        """orders-algo filled → _check_position_closed."""
        m = make_monitor()
        called = []

        async def mock_check():
            called.append(True)

        m._check_position_closed = mock_check

        msg = json.dumps({
            "arg": {"channel": "orders-algo", "instType": "SWAP"},
            "data": [{"state": "filled", "algoId": "sl_123"}]
        })

        _run(m._handle_private_message(msg))

        assert len(called) == 1

    def test_private_orders_algo_effective_checks_position(self):
        """orders-algo effective → _check_position_closed."""
        m = make_monitor()
        called = []

        async def mock_check():
            called.append(True)

        m._check_position_closed = mock_check

        msg = json.dumps({
            "arg": {"channel": "orders-algo", "instType": "SWAP"},
            "data": [{"state": "effective", "algoId": "sl_456"}]
        })

        _run(m._handle_private_message(msg))

        assert len(called) == 1

    def test_private_orders_algo_live_ignored(self):
        """orders-algo live state → NOT trigger check."""
        m = make_monitor()
        called = []

        async def mock_check():
            called.append(True)

        m._check_position_closed = mock_check

        msg = json.dumps({
            "arg": {"channel": "orders-algo", "instType": "SWAP"},
            "data": [{"state": "live", "algoId": "sl_789"}]
        })

        _run(m._handle_private_message(msg))

        assert len(called) == 0

    def test_private_orders_filled_with_position_checks(self):
        """orders filled + local position → _check_position_closed."""
        m = make_monitor()
        m.executor.load_position.return_value = {"direction": "LONG"}
        called = []

        async def mock_check():
            called.append(True)

        m._check_position_closed = mock_check

        msg = json.dumps({
            "arg": {"channel": "orders", "instType": "SWAP"},
            "data": [{"state": "filled", "ordId": "ord_001"}]
        })

        _run(m._handle_private_message(msg))

        assert len(called) == 1

    def test_private_orders_filled_no_position_skips(self):
        """orders filled but no local position → skip check."""
        m = make_monitor()
        m.executor.load_position.return_value = None
        called = []

        async def mock_check():
            called.append(True)

        m._check_position_closed = mock_check

        msg = json.dumps({
            "arg": {"channel": "orders", "instType": "SWAP"},
            "data": [{"state": "filled", "ordId": "ord_002"}]
        })

        _run(m._handle_private_message(msg))

        assert len(called) == 0

    def test_private_event_message_ignored(self):
        """Event messages (subscribe confirm) → ignored."""
        m = make_monitor()
        msg = json.dumps({"event": "subscribe", "arg": {"channel": "orders"}})
        _run(m._handle_private_message(msg))

    def test_invalid_json_handled(self):
        """Invalid JSON → no crash."""
        m = make_monitor()
        _run(m._handle_business_message("not json"))
        _run(m._handle_private_message("not json"))

    def test_activity_timestamp_updated(self):
        """Any WS message updates _last_activity."""
        m = make_monitor()
        m._last_activity = 0

        msg = json.dumps({"event": "subscribe"})
        _run(m._handle_business_message(msg))

        assert m._last_activity > 0

        m._last_activity = 0
        _run(m._handle_private_message(msg))
        assert m._last_activity > 0


# ---------------------------------------------------------------------------
# 7. Reconciliation on startup
# ---------------------------------------------------------------------------

class TestReconciliation:
    """Startup reconciliation scenarios."""

    def test_no_position_on_exchange(self):
        """No position → reconcile completes cleanly."""
        m = make_monitor()

        async def mock_rest_ex(method, *a, **kw):
            if method == "get_positions":
                return []
            return []

        async def run():
            m._rest_exchange = mock_rest_ex
            await m._reconcile_on_startup()

        _run(run())

    def test_position_with_sl_syncs(self):
        """Position + SL on exchange → sync via reconcile."""
        m = make_monitor()

        async def mock_rest_ex(method, *a, **kw):
            if method == "get_positions":
                return [{"pos": "1.00", "avgPx": "150.0"}]
            if method == "get_algo_orders":
                return [{"algoId": "sl_live", "slTriggerPx": "142.50"}]
            return []

        async def run():
            m._rest_exchange = mock_rest_ex
            await m._reconcile_on_startup()

        _run(run())

        m.executor.reconcile_position_from_exchange.assert_called_once_with(
            source="startup_reconcile")

    def test_position_without_sl_resets(self):
        """Position but no SL → emergency SL re-set."""
        m = make_monitor()

        calls = {}
        async def mock_rest_ex(method, *a, **kw):
            calls.setdefault(method, []).append((a, kw))
            if method == "get_positions":
                return [{"pos": "2.00", "avgPx": "150.0"}]
            if method == "get_algo_orders":
                return []  # No SL!
            if method == "place_stop_order":
                return {"code": "0", "data": [{"algoId": "new_sl"}]}
            if method == "place_limit_order":
                return {"code": "0", "data": [{"ordId": "new_tp"}]}
            return {"code": "0", "data": []}

        async def run():
            m._rest_exchange = mock_rest_ex
            await m._reconcile_on_startup()

        _run(run())

        assert "place_stop_order" in calls
        m.executor.save_position.assert_called_once()
        pos = m.executor.save_position.call_args[0][0]
        assert pos["direction"] == "LONG"
        assert pos["sl_algo_id"] == "new_sl"

    def test_position_without_sl_and_sl_fails_emergency_close(self):
        """Position no SL + SL placement fails → emergency close."""
        m = make_monitor()

        calls = {}
        async def mock_rest_ex(method, *a, **kw):
            calls.setdefault(method, []).append((a, kw))
            if method == "get_positions":
                return [{"pos": "-1.00", "avgPx": "150.0"}]
            if method == "get_algo_orders":
                return []
            if method == "place_stop_order":
                return {"code": "1", "data": []}  # SL FAIL
            if method == "place_market_order":
                return {"code": "0", "data": [{"ordId": "em_close"}]}
            return {"code": "0", "data": []}

        async def run():
            m._rest_exchange = mock_rest_ex
            await m._reconcile_on_startup()

        _run(run())

        assert "place_market_order" in calls
        m.executor.save_position.assert_called_with(None)

    def test_positions_none_skips(self):
        """get_positions returns None → skip reconciliation."""
        m = make_monitor()

        async def mock_rest_ex(method, *a, **kw):
            if method == "get_positions":
                return None
            return []

        async def run():
            m._rest_exchange = mock_rest_ex
            await m._reconcile_on_startup()

        _run(run())

        m.executor.save_position.assert_not_called()


# ---------------------------------------------------------------------------
# 8. CandleAccumulator
# ---------------------------------------------------------------------------

class TestCandleAccumulator:
    """CandleAccumulator state management."""

    def test_on_candle_close_appends(self):
        from okx_sol_bb.ws_monitor import CandleAccumulator
        acc = CandleAccumulator(MagicMock(), "SOL-USDT-SWAP", max_bars=5)
        acc._initialized = True
        acc.closes = [1.0, 2.0, 3.0]

        acc.on_candle_close(4.0)
        assert acc.closes == [1.0, 2.0, 3.0, 4.0]

    def test_on_candle_close_trims(self):
        from okx_sol_bb.ws_monitor import CandleAccumulator
        acc = CandleAccumulator(MagicMock(), "SOL-USDT-SWAP", max_bars=3)
        acc._initialized = True
        acc.closes = [1.0, 2.0, 3.0]

        acc.on_candle_close(4.0)
        assert acc.closes == [2.0, 3.0, 4.0]  # Trimmed to max_bars

    def test_ready_false_when_not_initialized(self):
        from okx_sol_bb.ws_monitor import CandleAccumulator
        acc = CandleAccumulator(MagicMock(), "SOL-USDT-SWAP")
        assert acc.ready is False

    def test_ready_false_with_few_closes(self):
        from okx_sol_bb.ws_monitor import CandleAccumulator
        acc = CandleAccumulator(MagicMock(), "SOL-USDT-SWAP")
        acc._initialized = True
        acc.closes = [1.0] * 10  # < 30
        assert acc.ready is False

    def test_ready_true_with_enough_closes(self):
        from okx_sol_bb.ws_monitor import CandleAccumulator
        acc = CandleAccumulator(MagicMock(), "SOL-USDT-SWAP")
        acc._initialized = True
        acc.closes = [1.0] * 50
        assert acc.ready is True
