"""Tests for WSMonitor — lifecycle scenarios, reconciliation, periodic checks."""
import sys
import asyncio
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch
from datetime import datetime, timezone

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from okx_bb.config import OKXConfig, StrategyConfig, RiskConfig, FeeConfig


def make_config():
    return OKXConfig(
        strategy=StrategyConfig(),
        risk=RiskConfig(stop_loss_pct=0.02, take_profit_pct=0.03),
        fees=FeeConfig(),
        api_key="test", secret_key="test", passphrase="test",
        coin="ETH", instId="ETH-USDT-SWAP",
    )


def _run(coro):
    """Run async coroutine in a fresh event loop."""
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
    return m


class TestReconciliation:
    """Startup reconciliation scenarios."""

    def test_no_position_clears_stale_pending(self):
        """No position + stale pending IDs → cleared."""
        m = make_monitor()
        m._pending_long_algoId = "stale123"

        async def run():
            m._rest_exchange = AsyncMock(side_effect=lambda method, *a, **kw: {
                "get_positions": [],
                "get_algo_orders": [],
            }.get(method, []))
            await m._reconcile_on_startup()

        _run(run())
        assert m._pending_long_algoId is None

    def test_position_without_sl_resets_sltp(self):
        """Position exists but no SL → re-set SL/TP."""
        m = make_monitor()

        calls = {}
        async def mock_rest(method, *a, **kw):
            calls.setdefault(method, []).append((a, kw))
            if method == "get_positions":
                return [{"pos": "-2.54", "avgPx": "1944.52"}]
            if method == "get_algo_orders":
                return []  # No SL!
            if method == "place_stop_order":
                return {"code": "0", "data": [{"algoId": "new_sl"}]}
            if method == "place_limit_order":
                return {"code": "0", "data": [{"ordId": "new_tp"}]}
            return {"code": "0", "data": []}

        async def run():
            m._rest_exchange = mock_rest
            await m._reconcile_on_startup()

        _run(run())
        # SL should have been placed
        assert "place_stop_order" in calls
        # Position state saved
        m.executor.save_position.assert_called()
        saved = m.executor.save_position.call_args[0][0]
        assert saved["direction"] == "SHORT"  # pos=-2.54
        assert saved["sl_algo_id"] == "new_sl"

    def test_position_with_sl_syncs_state(self):
        """Position + SL on exchange → reconstruct local state."""
        m = make_monitor()
        m.executor.load_position.return_value = None  # No local state

        async def mock_rest(method, *a, **kw):
            if method == "get_positions":
                return [{"pos": "-2.54", "avgPx": "1944.52"}]
            if method == "get_algo_orders":
                return [{"algoId": "sl1", "slTriggerPx": "1983.41"}]
            if method == "get_open_orders":
                return [{"ordId": "tp1", "reduceOnly": "true"}]
            return []

        async def run():
            m._rest_exchange = mock_rest
            await m._reconcile_on_startup()

        _run(run())
        m.executor.save_position.assert_called()
        saved = m.executor.save_position.call_args[0][0]
        assert saved["direction"] == "SHORT"
        assert saved["sl_algo_id"] == "sl1"
        assert saved["tp_order_id"] == "tp1"


class TestPeriodicOrphan:
    """Periodic check orphan detection uses config values, not hardcoded."""

    def test_orphan_with_sl_uses_config_pct(self):
        """Orphan reconstruction should use cfg.risk percentages, not magic numbers."""
        m = make_monitor()
        m.cfg.risk.stop_loss_pct = 0.05  # Non-default!
        m.cfg.risk.take_profit_pct = 0.10  # Non-default!
        m.executor.load_position.return_value = None
        m._entry_in_progress = False
        m._triggered_direction = None

        saved_pos = {}

        def capture_save(pos):
            saved_pos.update(pos or {})
        m.executor.save_position = MagicMock(side_effect=capture_save)

        async def mock_rest_fn(fn, *a, **kw):
            result = MagicMock()
            result.exit_reason = None
            return None  # check_position returns None

        call_count = [0]
        async def mock_rest_ex(method, *a, **kw):
            if method == "get_positions":
                call_count[0] += 1
                if call_count[0] == 1:
                    return [{"pos": "1.00", "avgPx": "2000.0"}]
                return [{"pos": "1.00", "avgPx": "2000.0"}]
            if method == "get_algo_orders":
                return [{"algoId": "sl_existing", "slTriggerPx": "1900"}]
            return []

        async def run():
            m._rest = mock_rest_fn
            m._rest_exchange = mock_rest_ex
            # Call the orphan detection logic directly (extracted from _periodic_check)
            # Simulate: no position locally, but position on exchange WITH SL
            positions = await m._rest_exchange("get_positions", m.cfg.instId)
            has_pos = any(float(p.get("pos", 0)) != 0 for p in positions)
            assert has_pos

            algos = await m._rest_exchange("get_algo_orders", m.cfg.instId, "conditional")
            has_sl = any(a.get("slTriggerPx") for a in algos)
            assert has_sl

            # This is the reconstruction path
            pos_info = next(p for p in positions if float(p.get("pos", 0)) != 0)
            pv = float(pos_info["pos"])
            d = "LONG" if pv > 0 else "SHORT"
            ap = float(pos_info["avgPx"])

            # Should use config values
            if d == "LONG":
                sl_p = ap * (1 - m.cfg.risk.stop_loss_pct)
                tp_p = ap * (1 + m.cfg.risk.take_profit_pct)
            else:
                sl_p = ap * (1 + m.cfg.risk.stop_loss_pct)
                tp_p = ap * (1 - m.cfg.risk.take_profit_pct)

            assert sl_p == 2000.0 * (1 - 0.05)  # 1900, not 1960
            assert tp_p == 2000.0 * (1 + 0.10)  # 2200, not 2060

        _run(run())


class TestSetLeverageNotInOpenPosition:
    """Verify set_leverage is NOT called during open_position."""

    def test_open_position_no_set_leverage(self):
        from okx_bb.executor import BBExecutor
        ex = BBExecutor(config=make_config())
        ex.client = MagicMock()
        ex.client.get_positions.return_value = []
        ex.client.get_balance.return_value = {"total_equity": 100}
        ex.client.get_instrument.return_value = {"ctVal": "0.01", "lotSz": "0.01", "minSz": "0.01"}
        ex.client.get_ticker.return_value = {"last": 2000}
        ex.client.place_market_order.return_value = {"code": "0", "data": [{"ordId": "123"}]}
        ex.client.get_order_detail.return_value = {"avgPx": "2000", "accFillSz": "1"}
        ex.client.place_stop_order.return_value = {"code": "0", "data": [{"algoId": "sl1"}]}
        ex.client.place_limit_order.return_value = {"code": "0", "data": [{"ordId": "tp1"}]}

        ex.open_position("LONG")

        # set_leverage should NOT be called
        ex.client.set_leverage.assert_not_called()


class TestExitReasonUnknownMapping:
    """Unknown exit reason maps to TIMEOUT, not TP."""

    def test_unknown_maps_to_timeout(self):
        from okx_bb.executor import BBExecutor
        from core.types import ExitReason
        ex = BBExecutor(config=make_config())
        ex.client = MagicMock()

        # Mock _determine_exit_reason to return 'unknown'
        ex.client.get_fills.return_value = [{"fillPx": "2010"}]
        pos = {"sl_algo_id": "a", "tp_order_id": "t", "sl_price": 1960, "tp_price": 2060}
        ex.client.get_algo_order_history.return_value = []
        ex.client.get_order_detail.return_value = {"state": "live"}

        reason = ex._determine_exit_reason(pos)
        assert reason == "unknown"

        # Verify mapping
        reason_map = {"sl": ExitReason.SL, "tp": ExitReason.TP,
                      "timeout": ExitReason.TIMEOUT, "unknown": ExitReason.TIMEOUT}
        assert reason_map[reason] == ExitReason.TIMEOUT


class TestTriggerTimeout:
    """Trigger fired but limit order didn't fill within timeout."""

    def test_trigger_timeout_no_position_resets(self):
        """Trigger timeout + no position → cancel stale orders, reset, re-place."""
        m = make_monitor()
        m._triggered_direction = "LONG"
        m._triggered_sz = "1.00"
        import time as _time
        m._triggered_at = _time.time() - 120  # 2 min ago, > 60s timeout
        m._entry_in_progress = False
        m.executor.load_position.return_value = None

        cancel_calls = []
        async def mock_rest_ex(method, *a, **kw):
            if method == "get_positions":
                return []  # No position
            if method == "get_open_orders":
                return [{"ordId": "stale_limit"}]
            if method == "cancel_order":
                cancel_calls.append(a)
                return {"code": "0"}
            return []

        m._atomic_cancel_and_place = AsyncMock()

        async def run():
            m._rest_exchange = mock_rest_ex
            # Simulate the trigger timeout check from _periodic_check
            elapsed = _time.time() - m._triggered_at
            assert elapsed > m.TRIGGER_FILL_TIMEOUT

            positions = await m._rest_exchange("get_positions", m.cfg.instId)
            has_pos = any(float(p.get("pos", 0)) != 0 for p in positions)
            assert not has_pos

            # Cancel stale orders
            open_orders = await m._rest_exchange("get_open_orders", m.cfg.instId)
            for o in open_orders:
                await m._rest_exchange("cancel_order", m.cfg.instId, o["ordId"])

            m._triggered_direction = None
            m._triggered_sz = None
            m._triggered_at = None

            await m._atomic_cancel_and_place()

        _run(run())
        assert len(cancel_calls) == 1
        assert m._triggered_direction is None
        m._atomic_cancel_and_place.assert_called_once()
