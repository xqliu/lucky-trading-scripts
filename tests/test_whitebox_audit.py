"""
Whitebox Audit Bug Reproduction Tests
=======================================
Tests that assert CORRECT behavior. They FAIL on current code (proving bugs exist).
After fixing each bug, the corresponding test will PASS.

Run: pytest tests/test_whitebox_audit.py -v
Expected: all FAIL until bugs are fixed.

Issues covered:
  1+2: executor open_position() should verify SL is live after placement
  3:   _determine_exit_reason should handle >0.5% slippage
  6:   periodic SL re-set should verify SL is live
  8:   _emergency_close should use exchange position size, not caller sz
"""
import sys
import asyncio
import time
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch, call
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


def _setup_open_mocks(ex, entry_price=2000.0, sz="1.00"):
    ex.client.get_positions.return_value = []
    ex.client.get_balance.return_value = {"total_equity": 1000}
    ex.client.get_instrument.return_value = {"ctVal": "0.01", "lotSz": "0.01", "minSz": "0.01"}
    ex.client.get_ticker.return_value = {"last": entry_price}
    ex.client.place_market_order.return_value = {"code": "0", "data": [{"ordId": "ord-123"}]}
    ex.client.get_order_detail.return_value = {"avgPx": str(entry_price), "accFillSz": sz}
    ex.client.place_stop_order.return_value = {"code": "0", "data": [{"algoId": "algo-sl-001"}]}
    ex.client.place_limit_order.return_value = {"code": "0", "data": [{"ordId": "ord-tp-001"}]}


# ============================================================================
# Issue 1+2: open_position() should verify SL live — currently doesn't
# ============================================================================


@patch("okx_bb.executor.send_discord")
@patch("core.notify.send_discord")
@patch("time.sleep")
def test_eth_open_position_detects_sl_not_live(mock_sleep, mock_n1, mock_n2, tmp_path):
    """ETH: SL placement returns code=0 but SL never appears live.
    open_position() should detect this and return False (emergency close)."""
    ex = _make_eth_executor()
    _setup_open_mocks(ex)
    # SL placement "succeeds" but is not actually live
    ex.client.get_algo_orders.return_value = []

    import okx_bb.executor as _mod
    orig_s, orig_l = _mod.POSITION_STATE_FILE, _mod.TRADE_LOG_FILE
    _mod.POSITION_STATE_FILE = tmp_path / "pos.json"
    _mod.TRADE_LOG_FILE = tmp_path / "log.json"
    try:
        result = ex.open_position("LONG")
        # CORRECT: should be False (SL not live → emergency close)
        assert result is False
    finally:
        _mod.POSITION_STATE_FILE = orig_s
        _mod.TRADE_LOG_FILE = orig_l


@patch("okx_sol_bb.executor.send_discord")
@patch("core.notify.send_discord")
@patch("time.sleep")
def test_sol_open_position_detects_sl_not_live(mock_sleep, mock_n1, mock_n2, tmp_path):
    """SOL: same — should return False when SL not live."""
    ex = _make_sol_executor()
    _setup_open_mocks(ex, entry_price=150.0)
    ex.client.get_instrument.return_value = {"ctVal": "1", "lotSz": "1", "minSz": "1"}
    ex.client.get_algo_orders.return_value = []

    import okx_sol_bb.executor as _mod
    orig_s, orig_l = _mod.POSITION_STATE_FILE, _mod.TRADE_LOG_FILE
    _mod.POSITION_STATE_FILE = tmp_path / "pos.json"
    _mod.TRADE_LOG_FILE = tmp_path / "log.json"
    try:
        result = ex.open_position("LONG")
        assert result is False
    finally:
        _mod.POSITION_STATE_FILE = orig_s
        _mod.TRADE_LOG_FILE = orig_l


# ============================================================================
# Issue 3: _determine_exit_reason should identify SL with >0.5% slippage
# ============================================================================


@patch("okx_bb.executor.send_discord")
@patch("core.notify.send_discord")
def test_eth_exit_reason_handles_slippage(mock_n1, mock_n2):
    """ETH: SL triggered with 0.8% slippage → should return 'sl', not 'unknown'."""
    ex = _make_eth_executor()
    sl_price = 1960.0
    fill_px = sl_price * (1 - 0.008)  # 0.8% past SL

    pos = {
        "direction": "LONG", "entry_price": 2000.0, "size": "1.00",
        "sl_price": sl_price, "tp_price": 2060.0,
        "sl_algo_id": "algo-sl-999", "tp_order_id": "ord-tp-999",
        "entry_time": datetime.now(timezone.utc).isoformat(),
    }
    ex.client.get_algo_order_history.return_value = []
    ex.client.get_order_detail.return_value = {"state": "live"}
    ex.client.get_fills.return_value = [{"fillPx": str(fill_px)}]

    reason = ex._determine_exit_reason(pos)
    # CORRECT: should be "sl" even with 0.8% slippage
    assert reason == "sl"


@patch("okx_sol_bb.executor.send_discord")
@patch("core.notify.send_discord")
def test_sol_exit_reason_handles_slippage(mock_n1, mock_n2):
    """SOL: same — should return 'sl' with 0.8% slippage."""
    ex = _make_sol_executor()
    sl_price = 142.5
    fill_px = sl_price * (1 - 0.008)

    pos = {
        "direction": "LONG", "entry_price": 150.0, "size": "1.00",
        "sl_price": sl_price, "tp_price": 153.0,
        "sl_algo_id": "algo-sl-sol", "tp_order_id": "ord-tp-sol",
        "entry_time": datetime.now(timezone.utc).isoformat(),
    }
    ex.client.get_algo_order_history.return_value = []
    ex.client.get_order_detail.return_value = {"state": "live"}
    ex.client.get_fills.return_value = [{"fillPx": str(fill_px)}]

    reason = ex._determine_exit_reason(pos)
    assert reason == "sl"


# ============================================================================
# Issue 6: periodic SL re-set should verify SL live after placement
# ============================================================================


def test_eth_periodic_sl_verifies_live():
    """ETH ws_monitor: after place_stop_order in periodic SL re-set,
    the code should call get_algo_orders to verify SL is actually live
    before saving state.

    We inspect the source code to verify this pattern exists.
    """
    import inspect
    from okx_bb.ws_monitor import WSMonitor

    source = inspect.getsource(WSMonitor._periodic_check)

    # Find the periodic SL re-set section: place_stop_order → verify get_algo_orders → save_position
    place_idx = source.find('"place_stop_order", self.cfg.instId, close_side, sz')
    save_idx = source.find('self.executor.save_position(local_pos)', place_idx)
    verify_idx = source.find('verify_algos = await self._rest_exchange("get_algo_orders", self.cfg.instId, "conditional")', place_idx)

    assert place_idx != -1, "Could not find periodic place_stop_order call"
    assert save_idx != -1, "Could not find save_position(local_pos) after periodic place_stop_order"
    assert verify_idx != -1, "No get_algo_orders verification after periodic place_stop_order"
    assert place_idx < verify_idx < save_idx, "Verification must happen between place_stop_order and save_position"


def test_sol_periodic_sl_verifies_live():
    """SOL ws_monitor: same — should verify SL live after re-set."""
    import inspect
    from okx_sol_bb.ws_monitor import WSMonitor

    source = inspect.getsource(WSMonitor._periodic_check)
    place_idx = source.find('"place_stop_order", self.cfg.instId, close_side, sz')
    save_idx = source.find('self.executor.save_position(local_pos)', place_idx)
    verify_idx = source.find('verify_algos = await self._rest_exchange("get_algo_orders", self.cfg.instId, "conditional")', place_idx)

    assert place_idx != -1, "Could not find periodic place_stop_order call"
    assert save_idx != -1, "Could not find save_position(local_pos) after periodic place_stop_order"
    assert verify_idx != -1, "No get_algo_orders verification after periodic place_stop_order"
    assert place_idx < verify_idx < save_idx, "Verification must happen between place_stop_order and save_position"


# ============================================================================
# Issue 8: _emergency_close should use exchange size, not caller sz
# ============================================================================


@patch("okx_bb.executor.send_discord")
@patch("core.notify.send_discord")
@patch("time.sleep")
def test_eth_emergency_close_uses_exchange_size(mock_sleep, mock_n1, mock_n2, tmp_path):
    """ETH: _emergency_close should query exchange for actual size.
    Caller passes sz=0.10 but actual position is 0.05."""
    import okx_bb.executor as _mod
    ex = _make_eth_executor()

    ex.client.get_positions.side_effect = [
        [{"pos": "0.05"}],  # actual position
        [],                  # after close: gone
        [],                  # save_position safety
    ]
    ex.client.place_market_order.return_value = {"code": "0", "data": [{"ordId": "c1"}]}

    orig = _mod.POSITION_STATE_FILE
    _mod.POSITION_STATE_FILE = tmp_path / "pos.json"
    try:
        result = ex._emergency_close("sell", "0.10")
        # CORRECT: should succeed by using exchange size 0.05
        assert result is True
        # CORRECT: should have sent 0.05 to OKX, not 0.10
        actual_sz = ex.client.place_market_order.call_args[0][2]
        assert actual_sz == "0.05", f"Should use exchange size 0.05, got {actual_sz}"
    finally:
        _mod.POSITION_STATE_FILE = orig


@patch("okx_sol_bb.executor.send_discord")
@patch("core.notify.send_discord")
@patch("time.sleep")
def test_sol_emergency_close_uses_exchange_size(mock_sleep, mock_n1, mock_n2, tmp_path):
    """SOL: same — should use exchange size 5.00, not caller's 10.00."""
    import okx_sol_bb.executor as _mod
    ex = _make_sol_executor()

    ex.client.get_positions.side_effect = [
        [{"pos": "5.00"}],
        [],
        [],
    ]
    ex.client.place_market_order.return_value = {"code": "0", "data": [{"ordId": "c1"}]}

    orig = _mod.POSITION_STATE_FILE
    _mod.POSITION_STATE_FILE = tmp_path / "pos.json"
    try:
        result = ex._emergency_close("sell", "10.00")
        assert result is True
        actual_sz = ex.client.place_market_order.call_args[0][2]
        assert actual_sz == "5.00", f"Should use exchange size 5.00, got {actual_sz}"
    finally:
        _mod.POSITION_STATE_FILE = orig
