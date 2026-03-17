"""
Whitebox Audit Bug Reproduction Tests
=======================================
Tests that REPRODUCE bugs discovered during whitebox code review (2026-03-17).
All tests PASS by asserting the current (buggy) behavior.

When a bug is fixed, the corresponding test must be updated to assert correct behavior.

Issues covered:
  1+2: executor open_position() does not verify SL is live after placement
  3:   _determine_exit_reason 0.5% tolerance too narrow for real slippage
  6:   periodic SL re-set does not verify SL is actually live
  8:   _emergency_close uses caller-supplied sz instead of exchange position size
"""
import sys
import asyncio
import time
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch
from datetime import datetime, timezone

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from okx_bb.config import (
    OKXConfig,
    StrategyConfig as EthStrategyConfig,
    RiskConfig as EthRiskConfig,
    FeeConfig as EthFeeConfig,
    ExecutionConfig,
)
from okx_sol_bb.config import (
    OKXSolConfig,
    StrategyConfig as SolStrategyConfig,
    RiskConfig as SolRiskConfig,
    FeeConfig as SolFeeConfig,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_eth_config():
    return OKXConfig(
        strategy=EthStrategyConfig(bb_period=20, bb_multiplier=2.5,
                                   trend_ema_period=96, trend_lookback=8),
        risk=EthRiskConfig(stop_loss_pct=0.02, take_profit_pct=0.03),
        fees=EthFeeConfig(),
        api_key="test", secret_key="test", passphrase="test",
        coin="ETH", instId="ETH-USDT-SWAP",
        execution=ExecutionConfig(mode="close_confirm_buffer"),
    )


def _make_sol_config():
    return OKXSolConfig(
        strategy=SolStrategyConfig(bb_period=14, bb_multiplier=3.0),
        risk=SolRiskConfig(stop_loss_pct=0.05, take_profit_pct=0.02, leverage=5),
        fees=SolFeeConfig(),
        api_key="test", secret_key="test", passphrase="test",
        coin="SOL", instId="SOL-USDT-SWAP",
    )


def _make_eth_executor():
    from okx_bb.executor import BBExecutor
    ex = BBExecutor(config=_make_eth_config())
    ex.client = MagicMock()
    return ex


def _make_sol_executor():
    from okx_sol_bb.executor import SolBBExecutor
    ex = SolBBExecutor(config=_make_sol_config())
    ex.client = MagicMock()
    return ex


def _setup_open_position_mocks(ex, entry_price=2000.0, sz="1.00"):
    """Configure mocks so open_position() succeeds up to SL placement."""
    ex.client.get_positions.return_value = []
    ex.client.get_balance.return_value = {"total_equity": 1000}
    ex.client.get_instrument.return_value = {"ctVal": "0.01", "lotSz": "0.01", "minSz": "0.01"}
    ex.client.get_ticker.return_value = {"last": entry_price}
    ex.client.place_market_order.return_value = {
        "code": "0", "data": [{"ordId": "ord-123"}]
    }
    ex.client.get_order_detail.return_value = {
        "avgPx": str(entry_price), "accFillSz": sz
    }
    ex.client.place_stop_order.return_value = {
        "code": "0", "data": [{"algoId": "algo-sl-001"}]
    }
    ex.client.place_limit_order.return_value = {
        "code": "0", "data": [{"ordId": "ord-tp-001"}]
    }


# ============================================================================
# Issue 1+2: open_position() does not verify SL live after placement
# ============================================================================


@patch("okx_bb.executor.send_discord")
@patch("core.notify.send_discord")
@patch("time.sleep")
def test_eth_open_position_sl_not_verified(mock_sleep, mock_notify1, mock_notify2, tmp_path):
    """ETH: place_stop_order returns code=0 but SL never appears in get_algo_orders.

    BUG: open_position() returns True and saves state without verifying SL is live.
    CORRECT: should detect SL not active → emergency close → return False.
    """
    ex = _make_eth_executor()
    _setup_open_position_mocks(ex, entry_price=2000.0)

    # SL placement returns success but SL is NOT actually live
    ex.client.get_algo_orders.return_value = []

    import okx_bb.executor as _mod
    orig_state = _mod.POSITION_STATE_FILE
    orig_log = _mod.TRADE_LOG_FILE
    _mod.POSITION_STATE_FILE = tmp_path / "position_state.json"
    _mod.TRADE_LOG_FILE = tmp_path / "trade_log.json"
    try:
        result = ex.open_position("LONG")
        # BUG: returns True — it never checks get_algo_orders after SL placement
        # After fix, this should be: assert result is False
        assert result is True, "BUG REPRO: open_position succeeds without SL live verification"
    finally:
        _mod.POSITION_STATE_FILE = orig_state
        _mod.TRADE_LOG_FILE = orig_log


@patch("okx_sol_bb.executor.send_discord")
@patch("core.notify.send_discord")
@patch("time.sleep")
def test_sol_open_position_sl_not_verified(mock_sleep, mock_notify1, mock_notify2, tmp_path):
    """SOL: same bug — open_position succeeds without SL live verification."""
    ex = _make_sol_executor()
    _setup_open_position_mocks(ex, entry_price=150.0, sz="1.00")
    ex.client.get_instrument.return_value = {"ctVal": "1", "lotSz": "1", "minSz": "1"}

    ex.client.get_algo_orders.return_value = []  # SL not live

    import okx_sol_bb.executor as _mod
    orig_state = _mod.POSITION_STATE_FILE
    orig_log = _mod.TRADE_LOG_FILE
    _mod.POSITION_STATE_FILE = tmp_path / "position_state.json"
    _mod.TRADE_LOG_FILE = tmp_path / "trade_log.json"
    try:
        result = ex.open_position("LONG")
        # BUG: returns True without SL verification
        assert result is True, "BUG REPRO: SOL open_position succeeds without SL live verification"
    finally:
        _mod.POSITION_STATE_FILE = orig_state
        _mod.TRADE_LOG_FILE = orig_log


# ============================================================================
# Issue 3: _determine_exit_reason 0.5% tolerance too narrow for slippage
# ============================================================================


@patch("okx_bb.executor.send_discord")
@patch("core.notify.send_discord")
def test_eth_determine_exit_reason_narrow_tolerance(mock_n1, mock_n2):
    """ETH: SL triggered with 0.8% slippage → returns 'unknown' instead of 'sl'.

    BUG: 0.5% tolerance doesn't cover real-world slippage scenarios.
    CORRECT: should return 'sl' with up to ~1% tolerance.
    """
    ex = _make_eth_executor()

    sl_price = 1960.0
    fill_price_with_slippage = sl_price * (1 - 0.008)  # 0.8% past SL

    pos = {
        "direction": "LONG",
        "entry_price": 2000.0,
        "size": "1.00",
        "sl_price": sl_price,
        "tp_price": 2060.0,
        "sl_algo_id": "algo-sl-999",
        "tp_order_id": "ord-tp-999",
        "entry_time": datetime.now(timezone.utc).isoformat(),
    }

    ex.client.get_algo_order_history.return_value = []  # can't find by algoId
    ex.client.get_order_detail.return_value = {"state": "live"}  # TP not filled
    ex.client.get_fills.return_value = [{"fillPx": str(fill_price_with_slippage)}]

    reason = ex._determine_exit_reason(pos)
    # BUG: returns "unknown" because 0.8% > 0.5% tolerance
    # After fix: assert reason == "sl"
    assert reason == "unknown", f"BUG REPRO: 0.8% slippage returns '{reason}', should be 'sl'"


@patch("okx_sol_bb.executor.send_discord")
@patch("core.notify.send_discord")
def test_sol_determine_exit_reason_narrow_tolerance(mock_n1, mock_n2):
    """SOL: same tolerance bug — 0.8% slippage returns 'unknown'."""
    ex = _make_sol_executor()

    sl_price = 142.5
    fill_price_with_slippage = sl_price * (1 - 0.008)

    pos = {
        "direction": "LONG",
        "entry_price": 150.0,
        "size": "1.00",
        "sl_price": sl_price,
        "tp_price": 153.0,
        "sl_algo_id": "algo-sl-sol-999",
        "tp_order_id": "ord-tp-sol-999",
        "entry_time": datetime.now(timezone.utc).isoformat(),
    }

    ex.client.get_algo_order_history.return_value = []
    ex.client.get_order_detail.return_value = {"state": "live"}
    ex.client.get_fills.return_value = [{"fillPx": str(fill_price_with_slippage)}]

    reason = ex._determine_exit_reason(pos)
    # BUG: returns "unknown"
    assert reason == "unknown", f"BUG REPRO: SOL 0.8% slippage returns '{reason}'"


# ============================================================================
# Issue 6: periodic SL re-set does not verify SL is live after placement
# ============================================================================


@patch("okx_bb.ws_monitor.send_discord", new_callable=AsyncMock)
@patch("core.notify.send_discord")
def test_eth_periodic_sl_reset_not_verified(mock_n1, mock_ws_discord):
    """ETH ws_monitor: periodic check re-sets SL, gets code=0, saves state —
    but never verifies SL is actually live on exchange.

    BUG: save_position is called with sl_algo_id even though SL may not be active.
    CORRECT: should verify via get_algo_orders before saving.
    """
    from okx_bb.ws_monitor import WSMonitor
    m = WSMonitor(config=_make_eth_config())
    m._loop = asyncio.new_event_loop()
    m.executor = MagicMock()
    m._entry_in_progress = False

    local_pos = {
        "direction": "LONG", "entry_price": 2000.0, "size": "1.00",
        "sl_price": 1960.0, "sl_algo_id": "old-algo", "tp_order_id": "tp-001",
        "entry_time": datetime.now(timezone.utc).isoformat(),
    }
    m.executor.load_position.return_value = local_pos
    m.executor.check_position.return_value = None

    rest_calls = []
    async def mock_rest_exchange(method, *args, **kwargs):
        rest_calls.append(method)
        if method == "get_algo_orders":
            return []  # SL never live
        if method == "get_positions":
            return [{"pos": "1.00", "avgPx": "2000.0"}]
        if method == "get_ticker":
            return {"last": 2050.0}
        if method == "place_stop_order":
            return {"code": "0", "data": [{"algoId": "new-algo-id"}]}
        return None

    m._rest_exchange = AsyncMock(side_effect=mock_rest_exchange)

    async def run_periodic_sl_check():
        # Replicate the periodic SL check logic from ws_monitor
        algos = await m._rest_exchange("get_algo_orders", m.cfg.instId, "conditional")
        has_sl = any(a.get("slTriggerPx") for a in (algos or []))
        if not has_sl:
            positions = await m._rest_exchange("get_positions", m.cfg.instId)
            pos_info = next(p for p in positions if float(p.get("pos", 0)) != 0)
            d = "LONG"
            ap = float(pos_info.get("avgPx", 0))
            sz = f"{abs(float(pos_info.get('pos', 0))):.2f}"
            close_side = "sell"
            sl_p = ap * (1 - m.cfg.risk.stop_loss_pct)

            ticker = await m._rest_exchange("get_ticker", m.cfg.instId)

            sl_result = await m._rest_exchange(
                "place_stop_order", m.cfg.instId, close_side, sz,
                slTriggerPx=f"{sl_p:.2f}")

            if sl_result and sl_result.get("code") == "0":
                local_pos["sl_algo_id"] = sl_result["data"][0].get("algoId", "")
                local_pos["sl_price"] = sl_p
                m.executor.save_position(local_pos)

    loop = asyncio.new_event_loop()
    loop.run_until_complete(run_periodic_sl_check())
    loop.close()

    # BUG: save_position was called (SL "set") but no verification call happened
    m.executor.save_position.assert_called_once()
    saved_pos = m.executor.save_position.call_args[0][0]
    assert saved_pos["sl_algo_id"] == "new-algo-id"

    # Count how many times get_algo_orders was called — should be 1 (initial check only)
    algo_calls = [c for c in rest_calls if c == "get_algo_orders"]
    # BUG: only 1 call (initial check). Should be 2 (initial + verification).
    # After fix: assert len(algo_calls) == 2
    assert len(algo_calls) == 1, f"BUG REPRO: get_algo_orders called {len(algo_calls)} times, should be 2 for verification"


@patch("okx_sol_bb.ws_monitor.send_discord", new_callable=AsyncMock)
@patch("core.notify.send_discord")
def test_sol_periodic_sl_reset_not_verified(mock_n1, mock_ws_discord):
    """SOL: same bug — periodic SL re-set saves state without live verification."""
    from okx_sol_bb.ws_monitor import WSMonitor
    m = WSMonitor(config=_make_sol_config())
    m._loop = asyncio.new_event_loop()
    m.executor = MagicMock()
    m._entry_in_progress = False

    local_pos = {
        "direction": "LONG", "entry_price": 150.0, "size": "1.00",
        "sl_price": 142.5, "sl_algo_id": "old-algo", "tp_order_id": "tp-001",
        "entry_time": datetime.now(timezone.utc).isoformat(),
    }
    m.executor.load_position.return_value = local_pos
    m.executor.check_position.return_value = None

    rest_calls = []
    async def mock_rest_exchange(method, *args, **kwargs):
        rest_calls.append(method)
        if method == "get_algo_orders":
            return []
        if method == "get_positions":
            return [{"pos": "1.00", "avgPx": "150.0"}]
        if method == "get_ticker":
            return {"last": 155.0}
        if method == "place_stop_order":
            return {"code": "0", "data": [{"algoId": "new-sol-algo"}]}
        return None

    m._rest_exchange = AsyncMock(side_effect=mock_rest_exchange)

    async def run_periodic_sl_check():
        algos = await m._rest_exchange("get_algo_orders", m.cfg.instId, "conditional")
        has_sl = any(a.get("slTriggerPx") for a in (algos or []))
        if not has_sl:
            positions = await m._rest_exchange("get_positions", m.cfg.instId)
            pos_info = next(p for p in positions if float(p.get("pos", 0)) != 0)
            ap = float(pos_info.get("avgPx", 0))
            sz = f"{abs(float(pos_info.get('pos', 0))):.2f}"
            sl_p = ap * (1 - m.cfg.risk.stop_loss_pct)

            await m._rest_exchange("get_ticker", m.cfg.instId)

            sl_result = await m._rest_exchange(
                "place_stop_order", m.cfg.instId, "sell", sz,
                slTriggerPx=f"{sl_p:.2f}")

            if sl_result and sl_result.get("code") == "0":
                local_pos["sl_algo_id"] = sl_result["data"][0].get("algoId", "")
                local_pos["sl_price"] = sl_p
                m.executor.save_position(local_pos)

    loop = asyncio.new_event_loop()
    loop.run_until_complete(run_periodic_sl_check())
    loop.close()

    m.executor.save_position.assert_called_once()
    algo_calls = [c for c in rest_calls if c == "get_algo_orders"]
    # BUG: only 1 call, should be 2
    assert len(algo_calls) == 1, f"BUG REPRO: SOL get_algo_orders called {len(algo_calls)} times"


# ============================================================================
# Issue 8: _emergency_close uses caller-supplied sz, not exchange position size
# ============================================================================


@patch("okx_bb.executor.send_discord")
@patch("core.notify.send_discord")
@patch("time.sleep")
def test_eth_emergency_close_wrong_size(mock_sleep, mock_n1, mock_n2, tmp_path):
    """ETH: _emergency_close("sell", "0.10") when actual position is 0.05.

    BUG: uses caller sz=0.10, OKX rejects reduceOnly sz > actual → 3 retries → fail.
    CORRECT: should query exchange for actual size and use that.
    """
    import okx_bb.executor as _mod
    ex = _make_eth_executor()

    # Exchange always shows 0.05
    ex.client.get_positions.return_value = [{"pos": "0.05"}]
    # OKX rejects sz > actual
    ex.client.place_market_order.return_value = {"code": "51000", "msg": "Parameter sz error"}

    orig = _mod.POSITION_STATE_FILE
    _mod.POSITION_STATE_FILE = tmp_path / "position_state.json"
    try:
        result = ex._emergency_close("sell", "0.10")
        # BUG: fails because it sends sz=0.10 instead of querying exchange for 0.05
        assert result is False, "BUG REPRO: emergency_close fails with wrong size"

        # Verify every attempt used the wrong size
        for call in ex.client.place_market_order.call_args_list:
            assert call[0][2] == "0.10", f"BUG REPRO: used {call[0][2]} instead of exchange size 0.05"
    finally:
        _mod.POSITION_STATE_FILE = orig


@patch("okx_bb.executor.send_discord")
@patch("core.notify.send_discord")
@patch("time.sleep")
def test_eth_emergency_close_correct_size_succeeds(mock_sleep, mock_n1, mock_n2, tmp_path):
    """ETH: contrast — with correct size (0.05), close succeeds."""
    import okx_bb.executor as _mod
    ex = _make_eth_executor()

    ex.client.get_positions.side_effect = [
        [{"pos": "0.05"}],  # check: still open
        [],                  # verify: closed
        [],                  # save_position safety check
    ]
    ex.client.place_market_order.return_value = {"code": "0", "data": [{"ordId": "c1"}]}

    orig = _mod.POSITION_STATE_FILE
    _mod.POSITION_STATE_FILE = tmp_path / "position_state.json"
    try:
        result = ex._emergency_close("sell", "0.05")
        assert result is True
    finally:
        _mod.POSITION_STATE_FILE = orig


@patch("okx_sol_bb.executor.send_discord")
@patch("core.notify.send_discord")
@patch("time.sleep")
def test_sol_emergency_close_wrong_size(mock_sleep, mock_n1, mock_n2, tmp_path):
    """SOL: same bug — _emergency_close uses caller sz, not exchange size."""
    import okx_sol_bb.executor as _mod
    ex = _make_sol_executor()

    ex.client.get_positions.return_value = [{"pos": "5.00"}]
    ex.client.place_market_order.return_value = {"code": "51000", "msg": "sz error"}

    orig = _mod.POSITION_STATE_FILE
    _mod.POSITION_STATE_FILE = tmp_path / "position_state.json"
    try:
        result = ex._emergency_close("sell", "10.00")
        assert result is False
        for call in ex.client.place_market_order.call_args_list:
            assert call[0][2] == "10.00", f"BUG REPRO: used {call[0][2]} instead of 5.00"
    finally:
        _mod.POSITION_STATE_FILE = orig


@patch("okx_sol_bb.executor.send_discord")
@patch("core.notify.send_discord")
@patch("time.sleep")
def test_sol_emergency_close_correct_size_succeeds(mock_sleep, mock_n1, mock_n2, tmp_path):
    """SOL: contrast — correct size succeeds."""
    import okx_sol_bb.executor as _mod
    ex = _make_sol_executor()

    ex.client.get_positions.side_effect = [
        [{"pos": "5.00"}],
        [],
        [],
    ]
    ex.client.place_market_order.return_value = {"code": "0", "data": [{"ordId": "c1"}]}

    orig = _mod.POSITION_STATE_FILE
    _mod.POSITION_STATE_FILE = tmp_path / "position_state.json"
    try:
        result = ex._emergency_close("sell", "5.00")
        assert result is True
    finally:
        _mod.POSITION_STATE_FILE = orig
