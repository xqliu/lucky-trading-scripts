"""Tests for SolBBExecutor — mock OKXClient to test execution logic."""
import sys
import json
from pathlib import Path
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from okx_sol_bb.executor import SolBBExecutor, POSITION_STATE_FILE, TRADE_LOG_FILE
from okx_sol_bb.config import OKXSolConfig, StrategyConfig, RiskConfig, FeeConfig
from core.types import ExitReason, Direction

# Block all Discord notifications during tests
import core.notify as _notify
_notify.send_discord = lambda *a, **kw: None


def make_config():
    return OKXSolConfig(
        strategy=StrategyConfig(bb_period=14, bb_multiplier=3.0),
        risk=RiskConfig(
            stop_loss_pct=0.05, take_profit_pct=0.02,
            max_hold_bars=96, position_ratio=0.30,
            max_single_loss=10.0, leverage=5,
        ),
        fees=FeeConfig(),
        api_key="test", secret_key="test", passphrase="test",
        coin="SOL", instId="SOL-USDT-SWAP",
    )


def make_executor():
    """Create executor with mocked client."""
    executor = SolBBExecutor(config=make_config())
    executor.client = MagicMock()
    return executor


# ── calculate_size ──────────────────────────────────────────────

class TestCalculateSize:
    def test_normal_sizing(self):
        ex = make_executor()
        ex.client.get_balance.return_value = {"total_equity": 100}
        ex.client.get_instrument.return_value = {"ctVal": "1", "lotSz": "1", "minSz": "1"}
        ex.client.get_ticker.return_value = {"last": 150.0}
        sz = ex.calculate_size()
        # notional = 100 * 0.30 = 30, max_loss = 30*0.05 = 1.5 <= 10 OK
        # contracts = 30 / (1 * 150) = 0.2 → int(0.2/1)*1 = 0 < minSz=1 → 1
        assert sz == "1.00"

    def test_no_equity(self):
        ex = make_executor()
        ex.client.get_balance.return_value = {"total_equity": 0}
        assert ex.calculate_size() is None

    def test_no_instrument(self):
        ex = make_executor()
        ex.client.get_balance.return_value = {"total_equity": 100}
        ex.client.get_instrument.return_value = None
        assert ex.calculate_size() is None

    def test_no_ticker(self):
        ex = make_executor()
        ex.client.get_balance.return_value = {"total_equity": 100}
        ex.client.get_instrument.return_value = {"ctVal": "1", "lotSz": "1", "minSz": "1"}
        ex.client.get_ticker.return_value = None
        assert ex.calculate_size() is None

    def test_max_single_loss_cap(self):
        """When position_ratio * equity would risk more than max_single_loss, cap it."""
        ex = make_executor()
        ex.client.get_balance.return_value = {"total_equity": 10000}
        ex.client.get_instrument.return_value = {"ctVal": "1", "lotSz": "1", "minSz": "1"}
        ex.client.get_ticker.return_value = {"last": 150.0}
        sz = ex.calculate_size()
        # notional = 10000 * 0.30 = 3000, max_loss = 3000*0.05 = 150 > 10
        # capped notional = 10 / 0.05 = 200
        # contracts = 200 / (1*150) = 1.33 → int(1.33/1)*1 = 1
        assert sz == "1.00"

    def test_large_equity_sizing(self):
        """With large equity and small price, multiple contracts."""
        ex = make_executor()
        ex.client.get_balance.return_value = {"total_equity": 5000}
        ex.client.get_instrument.return_value = {"ctVal": "0.1", "lotSz": "1", "minSz": "1"}
        ex.client.get_ticker.return_value = {"last": 10.0}
        sz = ex.calculate_size()
        # notional = min(5000*0.30, 10/0.05) = min(1500, 200) = 200
        # contracts = 200 / (0.1 * 10) = 200 → int(200/1)*1 = 200
        assert sz == "200.00"


# ── save_position ───────────────────────────────────────────────

class TestSavePosition:
    def test_save_non_none_skips_exchange_check(self, tmp_path):
        ex = make_executor()
        state_file = tmp_path / "position_state.json"
        pos = {"direction": "LONG", "entry_price": 150.0}
        with patch("okx_sol_bb.executor.POSITION_STATE_FILE", state_file):
            ex.save_position(pos)
            data = json.loads(state_file.read_text())
            assert data["position"]["direction"] == "LONG"
        # Should NOT call get_positions when saving non-None
        ex.client.get_positions.assert_not_called()

    def test_refuses_clear_if_api_fails(self, tmp_path):
        ex = make_executor()
        ex.client.get_positions.return_value = None
        state_file = tmp_path / "position_state.json"
        state_file.write_text('{"position": {"direction": "SHORT"}}')
        with patch("okx_sol_bb.executor.POSITION_STATE_FILE", state_file):
            ex.save_position(None)
            data = json.loads(state_file.read_text())
            assert data["position"] is not None

    def test_refuses_clear_if_exchange_has_position(self, tmp_path):
        ex = make_executor()
        ex.client.get_positions.return_value = [{"pos": "5"}]
        state_file = tmp_path / "position_state.json"
        state_file.write_text('{"position": {"direction": "LONG"}}')
        with patch("okx_sol_bb.executor.POSITION_STATE_FILE", state_file):
            ex.save_position(None)
            data = json.loads(state_file.read_text())
            assert data["position"] is not None

    def test_clears_when_exchange_flat(self, tmp_path):
        ex = make_executor()
        ex.client.get_positions.return_value = [{"pos": "0"}]
        state_file = tmp_path / "position_state.json"
        state_file.write_text('{"position": {"direction": "SHORT"}}')
        with patch("okx_sol_bb.executor.POSITION_STATE_FILE", state_file):
            ex.save_position(None)
            data = json.loads(state_file.read_text())
            assert data["position"] is None


# ── load_position ───────────────────────────────────────────────

class TestLoadPosition:
    def test_loads_existing(self, tmp_path):
        ex = make_executor()
        state_file = tmp_path / "position_state.json"
        pos = {"direction": "LONG", "entry_price": 155.0, "size": "2.00"}
        state_file.write_text(json.dumps({"position": pos}))
        with patch("okx_sol_bb.executor.POSITION_STATE_FILE", state_file):
            result = ex.load_position()
            assert result["direction"] == "LONG"
            assert result["entry_price"] == 155.0

    def test_returns_none_if_no_file(self, tmp_path):
        ex = make_executor()
        state_file = tmp_path / "nonexistent.json"
        with patch("okx_sol_bb.executor.POSITION_STATE_FILE", state_file):
            result = ex.load_position()
            assert result is None

    def test_returns_none_if_empty_state(self, tmp_path):
        ex = make_executor()
        state_file = tmp_path / "position_state.json"
        state_file.write_text(json.dumps({"position": None}))
        with patch("okx_sol_bb.executor.POSITION_STATE_FILE", state_file):
            result = ex.load_position()
            assert result is None


# ── check_position ──────────────────────────────────────────────

class TestCheckPosition:
    def test_returns_none_if_no_saved_position(self):
        ex = make_executor()
        ex.load_position = MagicMock(return_value=None)
        assert ex.check_position() is None

    def test_returns_none_if_api_fails(self):
        ex = make_executor()
        ex.load_position = MagicMock(return_value={
            "direction": "LONG",
            "entry_time": "2026-01-01T00:00:00+00:00",
        })
        ex.client.get_positions.return_value = None
        assert ex.check_position() is None

    def test_detects_exchange_close(self):
        """When exchange has no position, detect close and record result."""
        ex = make_executor()
        pos = {
            "direction": "LONG", "entry_price": 150.0, "size": "2",
            "sl_price": 142.5, "tp_price": 153.0,
            "sl_algo_id": "algo1", "tp_order_id": "ord1",
            "entry_time": "2026-01-01T00:00:00+00:00",
        }
        ex.load_position = MagicMock(return_value=pos)
        ex.client.get_positions.return_value = []  # Position closed
        ex._determine_exit_reason = MagicMock(return_value="tp")
        ex.save_position = MagicMock()
        ex._cancel_remaining_orders = MagicMock()
        # Mock _get_actual_exit_info for _record_closed_position
        ex.client.get_fills.return_value = [{"fillPx": "153.0", "ts": "1772979292595"}]
        ex.client.get_ticker.return_value = {"last": 153.0}

        result = ex.check_position()
        assert result is not None
        assert result.exit_reason == ExitReason.TP
        ex.save_position.assert_called_with(None)
        ex._cancel_remaining_orders.assert_called_once()

    def test_timeout_closes_and_cancels_orders(self):
        ex = make_executor()
        old_time = (datetime.now(timezone.utc) - timedelta(hours=100)).isoformat()
        pos = {
            "direction": "LONG", "entry_price": 150.0, "size": "2",
            "sl_price": 142.5, "tp_price": 153.0,
            "sl_algo_id": "algo1", "tp_order_id": "ord1",
            "entry_time": old_time,
        }
        ex.load_position = MagicMock(return_value=pos)
        ex.client.get_positions.return_value = [{"pos": "2"}]  # Still open
        ex._emergency_close = MagicMock(return_value=True)
        ex.client.get_fills.return_value = [{"fillPx": "148.0", "ts": "1772979292595"}]

        result = ex.check_position()
        assert result is not None
        assert result.exit_reason == ExitReason.TIMEOUT
        ex._emergency_close.assert_called_once()
        ex.client.cancel_algo_order.assert_called_once_with("algo1", "SOL-USDT-SWAP")
        ex.client.cancel_order.assert_called_once_with("SOL-USDT-SWAP", "ord1")

    def test_timeout_close_fails_preserves_orders(self):
        """If emergency close fails, SL/TP should NOT be cancelled."""
        ex = make_executor()
        old_time = (datetime.now(timezone.utc) - timedelta(hours=100)).isoformat()
        pos = {
            "direction": "LONG", "entry_price": 150.0, "size": "2",
            "sl_algo_id": "algo1", "tp_order_id": "ord1",
            "entry_time": old_time,
        }
        ex.load_position = MagicMock(return_value=pos)
        ex.client.get_positions.return_value = [{"pos": "2"}]
        ex._emergency_close = MagicMock(return_value=False)

        result = ex.check_position()
        assert result is None
        ex.client.cancel_algo_order.assert_not_called()
        ex.client.cancel_order.assert_not_called()

    def test_short_position_not_timed_out(self):
        """Position within timeout → no action."""
        ex = make_executor()
        recent = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        pos = {
            "direction": "SHORT", "entry_price": 150.0, "size": "2",
            "sl_price": 157.5, "tp_price": 147.0,
            "sl_algo_id": "algo1", "tp_order_id": "ord1",
            "entry_time": recent,
        }
        ex.load_position = MagicMock(return_value=pos)
        ex.client.get_positions.return_value = [{"pos": "-2"}]  # Still open
        result = ex.check_position()
        assert result is None


# ── reconcile_position_from_exchange ────────────────────────────

class TestReconcile:
    def test_rebuilds_long_position(self, tmp_path):
        ex = make_executor()
        ex.client.get_positions.return_value = [{
            "pos": "3", "avgPx": "155.0", "cTime": "1772595001158",
        }]
        ex.client.get_algo_orders.return_value = [{
            "algoId": "algo99", "side": "sell", "slTriggerPx": "147.25",
        }]
        ex.client.get_open_orders.return_value = [{
            "side": "sell", "reduceOnly": "true", "px": "158.1", "ordId": "tp99",
        }]
        state_file = tmp_path / "position_state.json"
        trade_log_file = tmp_path / "trade_log.json"

        with patch("okx_sol_bb.executor.POSITION_STATE_FILE", state_file), \
             patch("okx_sol_bb.executor.TRADE_LOG_FILE", trade_log_file):
            pos = ex.reconcile_position_from_exchange(source="test")
            assert pos["direction"] == "LONG"
            assert pos["entry_price"] == 155.0
            assert pos["sl_price"] == 147.25
            assert pos["tp_price"] == 158.1
            assert pos["sl_algo_id"] == "algo99"
            assert pos["tp_order_id"] == "tp99"
            assert pos["size"] == "3.00"

            # Check trade log
            log = json.loads(trade_log_file.read_text())
            open_rows = [x for x in log if x.get("status") == "OPEN"]
            assert len(open_rows) == 1
            assert open_rows[0]["direction"] == "LONG"

    def test_rebuilds_short_position(self, tmp_path):
        ex = make_executor()
        ex.client.get_positions.return_value = [{
            "pos": "-5", "avgPx": "160.0", "cTime": "1772595001158",
        }]
        ex.client.get_algo_orders.return_value = [{
            "algoId": "algo_sl", "side": "buy", "slTriggerPx": "168.0",
        }]
        ex.client.get_open_orders.return_value = [{
            "side": "buy", "reduceOnly": "true", "px": "156.8", "ordId": "tp_ord",
        }]
        state_file = tmp_path / "position_state.json"
        trade_log_file = tmp_path / "trade_log.json"

        with patch("okx_sol_bb.executor.POSITION_STATE_FILE", state_file), \
             patch("okx_sol_bb.executor.TRADE_LOG_FILE", trade_log_file):
            pos = ex.reconcile_position_from_exchange()
            assert pos["direction"] == "SHORT"
            assert pos["size"] == "5.00"
            assert pos["sl_price"] == 168.0
            assert pos["tp_price"] == 156.8

    def test_returns_local_if_api_fails(self, tmp_path):
        ex = make_executor()
        ex.client.get_positions.return_value = None
        state_file = tmp_path / "position_state.json"
        local_pos = {"direction": "LONG", "entry_price": 150.0}
        state_file.write_text(json.dumps({"position": local_pos}))
        with patch("okx_sol_bb.executor.POSITION_STATE_FILE", state_file):
            result = ex.reconcile_position_from_exchange()
            assert result == local_pos

    def test_clears_if_exchange_flat(self, tmp_path):
        ex = make_executor()
        ex.client.get_positions.return_value = [{"pos": "0"}]
        state_file = tmp_path / "position_state.json"
        state_file.write_text(json.dumps({"position": {"direction": "SHORT"}}))
        with patch("okx_sol_bb.executor.POSITION_STATE_FILE", state_file):
            result = ex.reconcile_position_from_exchange()
            assert result is None

    def test_no_sl_no_tp(self, tmp_path):
        """Reconcile handles missing SL/TP orders gracefully."""
        ex = make_executor()
        ex.client.get_positions.return_value = [{
            "pos": "2", "avgPx": "145.0", "cTime": "1772595001158",
        }]
        ex.client.get_algo_orders.return_value = []
        ex.client.get_open_orders.return_value = []
        state_file = tmp_path / "position_state.json"
        trade_log_file = tmp_path / "trade_log.json"

        with patch("okx_sol_bb.executor.POSITION_STATE_FILE", state_file), \
             patch("okx_sol_bb.executor.TRADE_LOG_FILE", trade_log_file):
            pos = ex.reconcile_position_from_exchange()
            assert pos["direction"] == "LONG"
            assert pos["sl_price"] is None
            assert pos["tp_price"] is None
            assert pos["sl_algo_id"] == ""
            assert pos["tp_order_id"] == ""


# ── open_position ───────────────────────────────────────────────

class TestOpenPosition:
    def test_aborts_if_existing_position(self):
        ex = make_executor()
        ex.client.get_positions.return_value = [{"pos": "1"}]
        assert ex.open_position("LONG") is False

    def test_aborts_if_positions_api_fails(self):
        ex = make_executor()
        ex.client.get_positions.return_value = None
        assert ex.open_position("LONG") is False

    def test_aborts_if_size_none(self):
        ex = make_executor()
        ex.client.get_positions.return_value = []
        ex.client.get_balance.return_value = {"total_equity": 0}
        assert ex.open_position("LONG") is False

    def test_aborts_if_market_order_fails(self):
        ex = make_executor()
        ex.client.get_positions.return_value = []
        ex.client.get_balance.return_value = {"total_equity": 100}
        ex.client.get_instrument.return_value = {"ctVal": "1", "lotSz": "1", "minSz": "1"}
        ex.client.get_ticker.return_value = {"last": 150}
        ex.client.place_market_order.return_value = {"code": "1", "msg": "fail"}
        assert ex.open_position("LONG") is False

    def test_aborts_if_no_ordId(self):
        ex = make_executor()
        ex.client.get_positions.return_value = []
        ex.client.get_balance.return_value = {"total_equity": 100}
        ex.client.get_instrument.return_value = {"ctVal": "1", "lotSz": "1", "minSz": "1"}
        ex.client.get_ticker.return_value = {"last": 150}
        ex.client.place_market_order.return_value = {"code": "0", "data": [{"ordId": ""}]}
        assert ex.open_position("LONG") is False

    @patch("time.sleep")
    def test_emergency_close_if_entry_price_zero(self, mock_sleep):
        ex = make_executor()
        ex.client.get_positions.return_value = []
        ex.client.get_balance.return_value = {"total_equity": 100}
        ex.client.get_instrument.return_value = {"ctVal": "1", "lotSz": "1", "minSz": "1"}
        ex.client.get_ticker.side_effect = [
            {"last": 150},  # calculate_size
            None,           # entry_price fallback
        ]
        ex.client.place_market_order.return_value = {"code": "0", "data": [{"ordId": "123"}]}
        ex.client.get_order_detail.return_value = None
        ex._emergency_close = MagicMock(return_value=True)

        assert ex.open_position("LONG") is False
        ex._emergency_close.assert_called_once()

    @patch("time.sleep")
    def test_emergency_close_if_sl_fails(self, mock_sleep):
        ex = make_executor()
        ex.client.get_positions.return_value = []
        ex.client.get_balance.return_value = {"total_equity": 100}
        ex.client.get_instrument.return_value = {"ctVal": "1", "lotSz": "1", "minSz": "1"}
        ex.client.get_ticker.return_value = {"last": 150}
        ex.client.place_market_order.return_value = {"code": "0", "data": [{"ordId": "123"}]}
        ex.client.get_order_detail.return_value = {"avgPx": "150", "accFillSz": "1"}
        ex.client.place_stop_order.return_value = {"code": "1", "msg": "fail"}
        ex._emergency_close = MagicMock(return_value=True)

        assert ex.open_position("LONG") is False
        ex._emergency_close.assert_called_once()

    @patch("time.sleep")
    def test_successful_long_open(self, mock_sleep):
        ex = make_executor()
        ex.client.get_positions.return_value = []
        ex.client.get_balance.return_value = {"total_equity": 100}
        ex.client.get_instrument.return_value = {"ctVal": "1", "lotSz": "1", "minSz": "1"}
        ex.client.get_ticker.return_value = {"last": 150.0}
        ex.client.place_market_order.return_value = {"code": "0", "data": [{"ordId": "ord1"}]}
        ex.client.get_order_detail.return_value = {"avgPx": "150.0", "accFillSz": "1"}
        ex.client.place_stop_order.return_value = {"code": "0", "data": [{"algoId": "sl1"}]}
        ex.client.place_limit_order.return_value = {"code": "0", "data": [{"ordId": "tp1"}]}
        ex.save_position = MagicMock()

        assert ex.open_position("LONG") is True
        ex.save_position.assert_called_once()
        saved = ex.save_position.call_args[0][0]
        assert saved["direction"] == "LONG"
        assert saved["entry_price"] == 150.0
        # SL = 150 * (1 - 0.05) = 142.5
        assert abs(saved["sl_price"] - 142.5) < 0.01
        # TP = 150 * (1 + 0.02) = 153.0
        assert abs(saved["tp_price"] - 153.0) < 0.01

    @patch("time.sleep")
    def test_successful_short_open(self, mock_sleep):
        ex = make_executor()
        ex.client.get_positions.return_value = []
        ex.client.get_balance.return_value = {"total_equity": 100}
        ex.client.get_instrument.return_value = {"ctVal": "1", "lotSz": "1", "minSz": "1"}
        ex.client.get_ticker.return_value = {"last": 150.0}
        ex.client.place_market_order.return_value = {"code": "0", "data": [{"ordId": "ord1"}]}
        ex.client.get_order_detail.return_value = {"avgPx": "150.0", "accFillSz": "1"}
        ex.client.place_stop_order.return_value = {"code": "0", "data": [{"algoId": "sl1"}]}
        ex.client.place_limit_order.return_value = {"code": "0", "data": [{"ordId": "tp1"}]}
        ex.save_position = MagicMock()

        assert ex.open_position("SHORT") is True
        saved = ex.save_position.call_args[0][0]
        assert saved["direction"] == "SHORT"
        # SL = 150 * (1 + 0.05) = 157.5
        assert abs(saved["sl_price"] - 157.5) < 0.01
        # TP = 150 * (1 - 0.02) = 147.0
        assert abs(saved["tp_price"] - 147.0) < 0.01

    @patch("time.sleep")
    def test_tp_fail_still_succeeds_with_sl(self, mock_sleep):
        """If TP order fails, position still opens (SL protects)."""
        ex = make_executor()
        ex.client.get_positions.return_value = []
        ex.client.get_balance.return_value = {"total_equity": 100}
        ex.client.get_instrument.return_value = {"ctVal": "1", "lotSz": "1", "minSz": "1"}
        ex.client.get_ticker.return_value = {"last": 150.0}
        ex.client.place_market_order.return_value = {"code": "0", "data": [{"ordId": "ord1"}]}
        ex.client.get_order_detail.return_value = {"avgPx": "150.0", "accFillSz": "1"}
        ex.client.place_stop_order.return_value = {"code": "0", "data": [{"algoId": "sl1"}]}
        ex.client.place_limit_order.return_value = {"code": "1", "msg": "tp fail"}
        ex.save_position = MagicMock()

        assert ex.open_position("LONG") is True
        saved = ex.save_position.call_args[0][0]
        assert saved["tp_order_id"] == ""  # TP failed but position opened


# ── emergency_close ─────────────────────────────────────────────

class TestEmergencyClose:
    @patch("time.sleep")
    def test_already_closed(self, mock_sleep):
        ex = make_executor()
        ex.client.get_positions.return_value = []
        ex.save_position = MagicMock()
        assert ex._emergency_close("sell", "1") is True

    @patch("time.sleep")
    def test_closes_after_market_order(self, mock_sleep):
        ex = make_executor()
        ex.client.get_positions.side_effect = [
            [{"pos": "1"}],  # before close
            [],               # after close
        ]
        ex.client.place_market_order.return_value = {"code": "0"}
        ex.save_position = MagicMock()
        assert ex._emergency_close("sell", "1") is True

    @patch("time.sleep")
    def test_all_attempts_fail(self, mock_sleep):
        ex = make_executor()
        ex.client.get_positions.return_value = [{"pos": "1"}]
        ex.client.place_market_order.return_value = {"code": "1", "msg": "fail"}
        assert ex._emergency_close("sell", "1") is False


# ── _determine_exit_reason ──────────────────────────────────────

class TestDetermineExitReason:
    def test_sl_triggered(self):
        ex = make_executor()
        pos = {"sl_algo_id": "algo1", "tp_order_id": "ord1",
               "sl_price": 142.5, "tp_price": 153.0}
        ex.client.get_algo_order_history.return_value = [
            {"algoId": "algo1", "state": "effective"}
        ]
        assert ex._determine_exit_reason(pos) == "sl"

    def test_tp_filled(self):
        ex = make_executor()
        pos = {"sl_algo_id": "algo1", "tp_order_id": "ord1",
               "sl_price": 142.5, "tp_price": 153.0}
        ex.client.get_algo_order_history.return_value = [
            {"algoId": "algo1", "state": "canceled"}
        ]
        ex.client.get_order_detail.return_value = {"state": "filled"}
        assert ex._determine_exit_reason(pos) == "tp"

    def test_unknown_fallback_to_fills_sl(self):
        ex = make_executor()
        pos = {"sl_algo_id": "algo1", "tp_order_id": "ord1",
               "sl_price": 142.5, "tp_price": 153.0}
        ex.client.get_algo_order_history.return_value = []
        ex.client.get_order_detail.return_value = {"state": "live"}
        ex.client.get_fills.return_value = [{"fillPx": "142.6"}]  # Close to SL
        assert ex._determine_exit_reason(pos) == "sl"

    def test_unknown_fallback_to_fills_tp(self):
        ex = make_executor()
        pos = {"sl_algo_id": "algo1", "tp_order_id": "ord1",
               "sl_price": 142.5, "tp_price": 153.0}
        ex.client.get_algo_order_history.return_value = []
        ex.client.get_order_detail.return_value = {"state": "live"}
        ex.client.get_fills.return_value = [{"fillPx": "153.1"}]  # Close to TP
        assert ex._determine_exit_reason(pos) == "tp"

    def test_truly_unknown(self):
        ex = make_executor()
        pos = {"sl_algo_id": "", "tp_order_id": "",
               "sl_price": 0, "tp_price": 0}
        ex.client.get_fills.return_value = [{"fillPx": "150"}]
        assert ex._determine_exit_reason(pos) == "unknown"


# ── _get_actual_exit_info ───────────────────────────────────────

class TestGetActualExitInfo:
    def test_uses_fill_data(self):
        ex = make_executor()
        ex.client.get_fills.return_value = [{
            "fillPx": "152.31", "ts": "1772979292595",
        }]
        price, exit_time = ex._get_actual_exit_info({"tp_order_id": ""})
        assert price == 152.31
        assert exit_time.year == 2026

    def test_fallback_to_tp_order_detail(self):
        ex = make_executor()
        ex.client.get_fills.return_value = []
        ex.client.get_order_detail.return_value = {
            "avgPx": "153.0", "uTime": "1772979292595",
        }
        price, exit_time = ex._get_actual_exit_info({"tp_order_id": "ord1"})
        assert price == 153.0

    def test_fallback_to_ticker(self):
        ex = make_executor()
        ex.client.get_fills.return_value = []
        ex.client.get_ticker.return_value = {"last": 151.0}
        price, exit_time = ex._get_actual_exit_info({"tp_order_id": ""})
        assert price == 151.0

    def test_final_fallback_to_entry_price(self):
        ex = make_executor()
        ex.client.get_fills.return_value = []
        ex.client.get_ticker.return_value = None
        price, exit_time = ex._get_actual_exit_info({
            "tp_order_id": "", "entry_price": 149.0,
        })
        assert price == 149.0


# ── _record_closed_position & _append_trade_log ────────────────

class TestRecordAndLog:
    def test_record_long_profit(self):
        ex = make_executor()
        pos = {
            "direction": "LONG", "entry_price": 150.0, "size": "2",
            "sl_price": 142.5, "tp_price": 153.0,
            "entry_time": "2026-01-01T00:00:00+00:00",
        }
        ex.client.get_fills.return_value = [{"fillPx": "153.0", "ts": "1772979292595"}]
        ex._append_trade_log = MagicMock()

        result = ex._record_closed_position(pos, "tp")
        assert result.exit_reason == ExitReason.TP
        assert result.pnl_pct > 0  # profit minus fees
        assert result.coin == "SOL"
        assert result.direction == Direction.LONG

    def test_record_short_profit(self):
        ex = make_executor()
        pos = {
            "direction": "SHORT", "entry_price": 160.0, "size": "3",
            "sl_price": 168.0, "tp_price": 156.8,
            "entry_time": "2026-01-01T00:00:00+00:00",
        }
        ex.client.get_fills.return_value = [{"fillPx": "156.8", "ts": "1772979292595"}]
        ex._append_trade_log = MagicMock()

        result = ex._record_closed_position(pos, "tp")
        assert result.pnl_pct > 0

    def test_append_trade_log_dedup(self, tmp_path):
        ex = make_executor()
        trade_log_file = tmp_path / "trade_log.json"
        trade_log_file.write_text("[]")

        from core.types import TradeResult
        result = TradeResult(
            coin="SOL", direction=Direction.SHORT,
            entry_price=160.0, exit_price=156.8,
            size=3.0, pnl_pct=0.019, pnl_usd=9.6,
            entry_time=datetime(2026, 3, 4, 3, 30, 1, tzinfo=timezone.utc),
            exit_time=datetime(2026, 3, 8, 14, 14, 52, tzinfo=timezone.utc),
            exit_reason=ExitReason.TP,
            strategy="bb_mean_reversion", fees_usd=0,
        )

        with patch("okx_sol_bb.executor.TRADE_LOG_FILE", trade_log_file):
            ex._append_trade_log(result)
            ex._append_trade_log(result)  # duplicate

        log = json.loads(trade_log_file.read_text())
        close_records = [r for r in log if r.get("exit_price") is not None]
        assert len(close_records) == 1


# ── position_status ─────────────────────────────────────────────

class TestPositionStatus:
    def test_no_position(self):
        ex = make_executor()
        ex.load_position = MagicMock(return_value=None)
        assert ex.position_status() == "No position"

    def test_long_position_display(self, tmp_path):
        ex = make_executor()
        pos = {
            "direction": "LONG", "entry_price": 150.0, "size": "2.00",
            "sl_price": 142.5, "tp_price": 153.0,
            "entry_time": "2026-03-01T10:00:00+00:00",
        }
        state_file = tmp_path / "position_state.json"
        state_file.write_text(json.dumps({"position": pos}))
        ex.client.get_ticker.return_value = {"last": 152.0}
        ex.client.get_balance.return_value = {"total_equity": 100}

        with patch("okx_sol_bb.executor.POSITION_STATE_FILE", state_file):
            status = ex.position_status()
        assert "LONG SOL @ $150.00" in status
        assert "SL: $142.50" in status
        assert "TP: $153.00" in status

    def test_none_tp_display(self, tmp_path):
        ex = make_executor()
        pos = {
            "direction": "SHORT", "entry_price": 160.0, "size": "3.00",
            "sl_price": 168.0, "tp_price": None,
            "entry_time": "2026-03-01T10:00:00+00:00",
        }
        state_file = tmp_path / "position_state.json"
        state_file.write_text(json.dumps({"position": pos}))
        ex.client.get_ticker.return_value = {"last": 155.0}
        ex.client.get_balance.return_value = {"total_equity": 200}

        with patch("okx_sol_bb.executor.POSITION_STATE_FILE", state_file):
            status = ex.position_status()
        assert "TP: None" in status
        assert "SHORT SOL @ $160.00" in status


# ── run_once ────────────────────────────────────────────────────

class TestRunOnce:
    def test_closed_position_returns_result(self):
        ex = make_executor()
        from core.types import TradeResult
        tr = TradeResult(
            coin="SOL", direction=Direction.LONG,
            entry_price=150.0, exit_price=153.0,
            size=1.0, pnl_pct=0.019, pnl_usd=3.0,
            entry_time=datetime.now(timezone.utc),
            exit_time=datetime.now(timezone.utc),
            exit_reason=ExitReason.TP, strategy="bb_mean_reversion",
        )
        ex.check_position = MagicMock(return_value=tr)
        result = ex.run_once()
        assert "Position closed" in result
        assert "TP" in result

    def test_existing_position_returns_status(self):
        ex = make_executor()
        ex.check_position = MagicMock(return_value=None)
        ex.load_position = MagicMock(return_value={"direction": "LONG"})
        ex.position_status = MagicMock(return_value="In position: LONG SOL")
        result = ex.run_once()
        assert "In position" in result

    def test_signal_detected_opens(self):
        ex = make_executor()
        ex.check_position = MagicMock(return_value=None)
        ex.load_position = MagicMock(return_value=None)
        ex.check_signal = MagicMock(return_value="LONG")
        ex.open_position = MagicMock(return_value=True)
        result = ex.run_once()
        assert "Opened LONG" in result

    def test_signal_detected_but_open_fails(self):
        ex = make_executor()
        ex.check_position = MagicMock(return_value=None)
        ex.load_position = MagicMock(return_value=None)
        ex.check_signal = MagicMock(return_value="SHORT")
        ex.open_position = MagicMock(return_value=False)
        result = ex.run_once()
        assert "open failed" in result

    def test_no_signal(self):
        ex = make_executor()
        ex.check_position = MagicMock(return_value=None)
        ex.load_position = MagicMock(return_value=None)
        ex.check_signal = MagicMock(return_value=None)
        result = ex.run_once()
        assert result == "No signal"


# ── _cancel_remaining_orders ────────────────────────────────────

class TestCancelRemainingOrders:
    def test_cancels_both(self):
        ex = make_executor()
        pos = {"sl_algo_id": "algo1", "tp_order_id": "ord1"}
        ex._cancel_remaining_orders(pos)
        ex.client.cancel_algo_order.assert_called_once_with("algo1", "SOL-USDT-SWAP")
        ex.client.cancel_order.assert_called_once_with("SOL-USDT-SWAP", "ord1")

    def test_skips_empty_ids(self):
        ex = make_executor()
        pos = {"sl_algo_id": "", "tp_order_id": ""}
        ex._cancel_remaining_orders(pos)
        ex.client.cancel_algo_order.assert_not_called()
        ex.client.cancel_order.assert_not_called()

    def test_handles_cancel_exception(self):
        ex = make_executor()
        pos = {"sl_algo_id": "algo1", "tp_order_id": "ord1"}
        ex.client.cancel_algo_order.side_effect = Exception("network error")
        ex.client.cancel_order.side_effect = Exception("network error")
        # Should not raise
        ex._cancel_remaining_orders(pos)
