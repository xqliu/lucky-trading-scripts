"""Tests for WSMonitor candle-close flow: _get_trend, _place_bb_orders_inner, _atomic_cancel_and_place."""
import sys
import asyncio
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from okx_bb.config import OKXConfig, StrategyConfig, RiskConfig, FeeConfig, ExecutionConfig


def make_config():
    return OKXConfig(
        strategy=StrategyConfig(bb_period=20, bb_multiplier=2.5,
                                trend_ema_period=96, trend_lookback=8),
        risk=RiskConfig(stop_loss_pct=0.02, take_profit_pct=0.03),
        fees=FeeConfig(),
        api_key="test", secret_key="test", passphrase="test",
        coin="ETH", instId="ETH-USDT-SWAP",
        execution=ExecutionConfig(mode="intrabar_trigger"),
    )


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def make_monitor():
    from okx_bb.ws_monitor import WSMonitor
    m = WSMonitor(config=make_config())
    m._loop = asyncio.new_event_loop()
    m.executor = MagicMock()
    m._pending_long_algoId = None
    m._pending_short_algoId = None
    m._entry_in_progress = False
    m._triggered_direction = None
    m._triggered_sz = None
    return m


# ---------------------------------------------------------------------------
# _get_trend
# ---------------------------------------------------------------------------

class TestGetTrend:
    """_get_trend: EMA-based trend detection."""

    def test_not_enough_closes_returns_none(self):
        m = make_monitor()
        m.accumulator._initialized = True
        m.accumulator.closes = [2000.0] * 10  # way too few
        assert m._get_trend() is None

    def test_uptrend_returns_up(self):
        """Steadily rising prices → 'up'."""
        m = make_monitor()
        m.accumulator._initialized = True
        # 300 prices: gradually increasing from 1800 to 2100
        m.accumulator.closes = [1800 + i * 1.0 for i in range(300)]
        assert m._get_trend() == "up"

    def test_downtrend_returns_down(self):
        """Steadily falling prices → 'down'."""
        m = make_monitor()
        m.accumulator._initialized = True
        m.accumulator.closes = [2100 - i * 1.0 for i in range(300)]
        assert m._get_trend() == "down"

    def test_flat_returns_none(self):
        """Perfectly flat prices → None (ema[-1] == ema[-1-lookback])."""
        m = make_monitor()
        m.accumulator._initialized = True
        m.accumulator.closes = [2000.0] * 300
        assert m._get_trend() is None


# ---------------------------------------------------------------------------
# _place_bb_orders_inner (the heart of _on_candle_close)
# ---------------------------------------------------------------------------

class TestPlaceBBOrdersInner:
    """_place_bb_orders_inner: BB trigger order placement."""

    def test_accumulator_not_ready_returns(self):
        """Accumulator not ready → no orders placed."""
        m = make_monitor()
        m.accumulator._initialized = False
        m._rest_exchange = AsyncMock()

        _run(m._place_bb_orders_inner())

        m._rest_exchange.assert_not_called()

    def test_bb_none_returns(self):
        """BB returns None (flat market) → no orders."""
        m = make_monitor()
        m.accumulator._initialized = True
        m.accumulator.closes = [2000.0] * 200  # ready but flat → BB=None

        m._rest_exchange = AsyncMock()

        with patch("okx_bb.ws_monitor.get_bb_levels", return_value=None):
            _run(m._place_bb_orders_inner())

        m._rest_exchange.assert_not_called()

    def test_trend_none_returns(self):
        """Trend unclear → no orders."""
        m = make_monitor()
        m.accumulator._initialized = True
        m.accumulator.closes = [2000.0] * 200

        m._rest_exchange = AsyncMock()

        with patch("okx_bb.ws_monitor.get_bb_levels", return_value=(2000, 2050, 1950)):
            with patch.object(m, "_get_trend", return_value=None):
                _run(m._place_bb_orders_inner())

        m._rest_exchange.assert_not_called()

    def test_uptrend_places_long_trigger(self):
        """trend='up', upper > price*1.001 → LONG trigger placed, algoId saved."""
        m = make_monitor()
        m.accumulator._initialized = True
        m.accumulator.closes = [2000.0] * 200

        trigger_result = {"code": "0", "data": [{"algoId": "algo_long_123"}]}

        async def mock_rest_exchange(method, *a, **kw):
            if method == "place_trigger_order":
                return trigger_result
            return {"code": "0", "data": []}

        m._rest_exchange = mock_rest_exchange

        async def mock_rest(fn):
            return fn()
        m._rest = mock_rest
        m.executor.calculate_size = MagicMock(return_value="0.1")

        with patch("okx_bb.ws_monitor.get_bb_levels", return_value=(2000, 2050, 1950)):
            with patch.object(m, "_get_trend", return_value="up"):
                _run(m._place_bb_orders_inner())

        assert m._pending_long_algoId == "algo_long_123"
        assert m._pending_short_algoId is None

    def test_downtrend_places_short_trigger(self):
        """trend='down', lower < price*0.999 → SHORT trigger placed, algoId saved."""
        m = make_monitor()
        m.accumulator._initialized = True
        m.accumulator.closes = [2000.0] * 200

        trigger_result = {"code": "0", "data": [{"algoId": "algo_short_456"}]}

        async def mock_rest_exchange(method, *a, **kw):
            if method == "place_trigger_order":
                return trigger_result
            return {"code": "0", "data": []}

        m._rest_exchange = mock_rest_exchange

        async def mock_rest(fn):
            return fn()
        m._rest = mock_rest
        m.executor.calculate_size = MagicMock(return_value="0.1")

        with patch("okx_bb.ws_monitor.get_bb_levels", return_value=(2000, 2050, 1950)):
            with patch.object(m, "_get_trend", return_value="down"):
                _run(m._place_bb_orders_inner())

        assert m._pending_short_algoId == "algo_short_456"
        assert m._pending_long_algoId is None

    def test_trigger_failure_logs_error_no_algoid(self):
        """Trigger placement fails → algoId stays None, error logged."""
        m = make_monitor()
        m.accumulator._initialized = True
        m.accumulator.closes = [2000.0] * 200

        trigger_result = {"code": "1", "msg": "Insufficient balance"}

        async def mock_rest_exchange(method, *a, **kw):
            if method == "place_trigger_order":
                return trigger_result
            return {"code": "0", "data": []}

        m._rest_exchange = mock_rest_exchange

        async def mock_rest(fn):
            return fn()
        m._rest = mock_rest
        m.executor.calculate_size = MagicMock(return_value="0.1")

        with patch("okx_bb.ws_monitor.get_bb_levels", return_value=(2000, 2050, 1950)):
            with patch.object(m, "_get_trend", return_value="up"):
                _run(m._place_bb_orders_inner())

        assert m._pending_long_algoId is None

    def test_calculate_size_returns_none_skips(self):
        """calculate_size returns falsy → no trigger placed."""
        m = make_monitor()
        m.accumulator._initialized = True
        m.accumulator.closes = [2000.0] * 200

        m._rest_exchange = AsyncMock()

        async def mock_rest(fn):
            return fn()
        m._rest = mock_rest
        m.executor.calculate_size = MagicMock(return_value=None)

        with patch("okx_bb.ws_monitor.get_bb_levels", return_value=(2000, 2050, 1950)):
            with patch.object(m, "_get_trend", return_value="up"):
                _run(m._place_bb_orders_inner())

        m._rest_exchange.assert_not_called()

    def test_uptrend_but_upper_too_close_skips(self):
        """trend='up' but upper <= price*1.001 → no trigger (condition not met)."""
        m = make_monitor()
        m.accumulator._initialized = True
        # price=2000, upper=2001 → 2001 <= 2000*1.001=2002 → skip
        m.accumulator.closes = [2000.0] * 200

        m._rest_exchange = AsyncMock()

        async def mock_rest(fn):
            return fn()
        m._rest = mock_rest
        m.executor.calculate_size = MagicMock(return_value="0.1")

        with patch("okx_bb.ws_monitor.get_bb_levels", return_value=(2000, 2001, 1950)):
            with patch.object(m, "_get_trend", return_value="up"):
                _run(m._place_bb_orders_inner())

        # place_trigger_order should not be called
        m._rest_exchange.assert_not_called()

    def test_downtrend_but_lower_too_close_skips(self):
        """trend='down' but lower >= price*0.999 → no trigger."""
        m = make_monitor()
        m.accumulator._initialized = True
        # price=2000, lower=1999 → 1999 >= 2000*0.999=1998 → skip
        m.accumulator.closes = [2000.0] * 200

        m._rest_exchange = AsyncMock()

        async def mock_rest(fn):
            return fn()
        m._rest = mock_rest
        m.executor.calculate_size = MagicMock(return_value="0.1")

        with patch("okx_bb.ws_monitor.get_bb_levels", return_value=(2000, 2050, 1999)):
            with patch.object(m, "_get_trend", return_value="down"):
                _run(m._place_bb_orders_inner())

        m._rest_exchange.assert_not_called()


# ---------------------------------------------------------------------------
# _atomic_cancel_and_place
# ---------------------------------------------------------------------------

class TestAtomicCancelAndPlace:
    """_atomic_cancel_and_place: cancel old triggers → place new ones."""

    def test_entry_in_progress_skips(self):
        """Entry in progress → skip entirely."""
        m = make_monitor()
        m._entry_in_progress = True
        m._rest_exchange = AsyncMock()

        _run(m._atomic_cancel_and_place())

        m._rest_exchange.assert_not_called()

    def test_triggered_direction_skips(self):
        """Trigger already fired → skip."""
        m = make_monitor()
        m._triggered_direction = "long"
        m._rest_exchange = AsyncMock()

        _run(m._atomic_cancel_and_place())

        m._rest_exchange.assert_not_called()

    def test_cancels_existing_then_places_new(self):
        """Has pending triggers → cancel them → place new via _place_bb_orders_inner."""
        m = make_monitor()
        m._pending_long_algoId = "old_long_1"
        m._pending_short_algoId = "old_short_2"

        cancel_calls = []

        async def mock_rest_exchange(method, *a, **kw):
            if method == "cancel_algo_order":
                cancel_calls.append(a[0])  # algoId
                return {"code": "0"}
            if method == "get_positions":
                return []
            return {"code": "0", "data": []}

        m._rest_exchange = mock_rest_exchange
        m.executor.load_position = MagicMock(return_value=None)

        place_called = []

        async def mock_place():
            place_called.append(True)
        m._place_bb_orders_inner = mock_place

        _run(m._atomic_cancel_and_place())

        assert "old_long_1" in cancel_calls
        assert "old_short_2" in cancel_calls
        assert m._pending_long_algoId is None
        assert m._pending_short_algoId is None
        assert place_called

    def test_cancel_failure_still_clears_and_continues(self):
        """Cancel raises → algoId still cleared, placement proceeds."""
        m = make_monitor()
        m._pending_long_algoId = "bad_algo"

        async def mock_rest_exchange(method, *a, **kw):
            if method == "cancel_algo_order":
                raise Exception("Network error")
            if method == "get_positions":
                return []
            return {"code": "0", "data": []}

        m._rest_exchange = mock_rest_exchange
        m.executor.load_position = MagicMock(return_value=None)

        place_called = []
        async def mock_place():
            place_called.append(True)
        m._place_bb_orders_inner = mock_place

        _run(m._atomic_cancel_and_place())

        assert m._pending_long_algoId is None
        assert place_called

    def test_position_exists_skips_placement(self):
        """Position already open → don't place new triggers."""
        m = make_monitor()

        async def mock_rest_exchange(method, *a, **kw):
            if method == "get_positions":
                return [{"pos": "2.0", "avgPx": "1900"}]
            return {"code": "0", "data": []}

        m._rest_exchange = mock_rest_exchange

        place_called = []
        async def mock_place():
            place_called.append(True)
        m._place_bb_orders_inner = mock_place

        _run(m._atomic_cancel_and_place())

        assert not place_called

    def test_executor_load_position_skips_placement(self):
        """Executor reports local position → skip placement."""
        m = make_monitor()

        async def mock_rest_exchange(method, *a, **kw):
            if method == "get_positions":
                return []
            return {"code": "0", "data": []}

        m._rest_exchange = mock_rest_exchange
        m.executor.load_position = MagicMock(return_value={"direction": "long"})

        place_called = []
        async def mock_place():
            place_called.append(True)
        m._place_bb_orders_inner = mock_place

        _run(m._atomic_cancel_and_place())

        assert not place_called

    def test_positions_none_skips_placement(self):
        """get_positions returns None → can't verify, skip placement."""
        m = make_monitor()

        async def mock_rest_exchange(method, *a, **kw):
            if method == "get_positions":
                return None
            return {"code": "0", "data": []}

        m._rest_exchange = mock_rest_exchange

        place_called = []
        async def mock_place():
            place_called.append(True)
        m._place_bb_orders_inner = mock_place

        _run(m._atomic_cancel_and_place())

        assert not place_called

    def test_trigger_fires_during_cancel_aborts(self):
        """Trigger fires between cancel and place → abort placement."""
        m = make_monitor()
        m._pending_long_algoId = "old_1"

        async def mock_rest_exchange(method, *a, **kw):
            if method == "cancel_algo_order":
                # Simulate trigger firing during await
                m._triggered_direction = "long"
                return {"code": "0"}
            if method == "get_positions":
                return []
            return {"code": "0", "data": []}

        m._rest_exchange = mock_rest_exchange
        m.executor.load_position = MagicMock(return_value=None)

        place_called = []
        async def mock_place():
            place_called.append(True)
        m._place_bb_orders_inner = mock_place

        _run(m._atomic_cancel_and_place())

        assert not place_called
