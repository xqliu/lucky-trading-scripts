"""Tests for BBExecutor — mock OKXClient to test execution logic."""
import sys
import json
from pathlib import Path
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from okx_bb.executor import BBExecutor, POSITION_STATE_FILE
from okx_bb.config import OKXConfig, StrategyConfig, RiskConfig, FeeConfig
from core.types import ExitReason

# Block all Discord notifications during tests
import core.notify as _notify
_notify.send_discord = lambda *a, **kw: None

# Also patch the already-imported reference in executor module
import okx_bb.executor as _executor
_executor.send_discord = lambda *a, **kw: None


def make_config():
    return OKXConfig(
        strategy=StrategyConfig(),
        risk=RiskConfig(stop_loss_pct=0.02, take_profit_pct=0.03, max_hold_bars=120),
        fees=FeeConfig(),
        api_key="test", secret_key="test", passphrase="test",
        coin="ETH", instId="ETH-USDT-SWAP",
    )


def make_executor(tmp_path=None):
    """Create executor with mocked client."""
    executor = BBExecutor(config=make_config())
    executor.client = MagicMock()
    return executor


class TestOpenPosition:
    def test_aborts_if_existing_position(self):
        ex = make_executor()
        ex.client.get_positions.return_value = [{"pos": "1"}]
        assert ex.open_position("LONG") is False

    def test_aborts_if_positions_api_fails(self):
        ex = make_executor()
        ex.client.get_positions.return_value = None  # API error
        assert ex.open_position("LONG") is False

    def test_aborts_if_no_equity(self):
        ex = make_executor()
        ex.client.get_positions.return_value = []
        ex.client.get_balance.return_value = {"total_equity": 0}
        assert ex.open_position("LONG") is False

    def test_aborts_if_market_order_fails(self):
        ex = make_executor()
        ex.client.get_positions.return_value = []
        ex.client.get_balance.return_value = {"total_equity": 100}
        ex.client.get_instrument.return_value = {"ctVal": "0.01"}
        ex.client.get_ticker.return_value = {"last": 2000}
        ex.client.set_leverage.return_value = {"code": "0"}
        ex.client.place_market_order.return_value = {"code": "1", "msg": "fail"}
        assert ex.open_position("LONG") is False

    def test_emergency_close_if_entry_price_zero(self):
        ex = make_executor()
        ex.client.get_positions.return_value = []
        ex.client.get_balance.return_value = {"total_equity": 100}
        ex.client.get_instrument.return_value = {"ctVal": "0.01"}
        # Ticker works for sizing, but returns None after market order
        ex.client.get_ticker.side_effect = [
            {"last": 2000},  # for calculate_size
            None,            # for entry_price fallback
        ]
        ex.client.set_leverage.return_value = {"code": "0"}
        ex.client.place_market_order.return_value = {
            "code": "0", "data": [{"ordId": "123"}]
        }
        ex.client.get_order_detail.return_value = None  # No fill info

        # Mock _emergency_close
        ex._emergency_close = MagicMock(return_value=True)

        assert ex.open_position("LONG") is False
        ex._emergency_close.assert_called_once()

    def test_emergency_close_if_sl_fails(self):
        ex = make_executor()
        ex.client.get_positions.return_value = []
        ex.client.get_balance.return_value = {"total_equity": 100}
        ex.client.get_instrument.return_value = {"ctVal": "0.01"}
        ex.client.get_ticker.return_value = {"last": 2000}
        ex.client.set_leverage.return_value = {"code": "0"}
        ex.client.place_market_order.return_value = {
            "code": "0", "data": [{"ordId": "123"}]
        }
        ex.client.get_order_detail.return_value = {"avgPx": "2000", "accFillSz": "1"}
        ex.client.place_stop_order.return_value = {"code": "1", "msg": "fail"}

        ex._emergency_close = MagicMock(return_value=True)

        assert ex.open_position("LONG") is False
        ex._emergency_close.assert_called_once()


class TestCheckPosition:
    def test_returns_none_if_no_saved_position(self):
        ex = make_executor()
        ex.load_position = MagicMock(return_value=None)
        assert ex.check_position() is None

    def test_returns_none_if_api_fails(self):
        ex = make_executor()
        ex.load_position = MagicMock(return_value={"direction": "LONG", "entry_time": "2026-01-01T00:00:00+00:00"})
        ex.client.get_positions.return_value = None  # API error
        assert ex.check_position() is None

    def test_detects_sl_tp_close(self):
        ex = make_executor()
        pos = {
            "direction": "LONG",
            "entry_price": 2000,
            "size": "1",
            "sl_price": 1960,
            "tp_price": 2060,
            "sl_algo_id": "algo123",
            "tp_order_id": "ord456",
            "entry_time": "2026-01-01T00:00:00+00:00",
        }
        ex.load_position = MagicMock(return_value=pos)
        ex.client.get_positions.return_value = []  # Position gone
        ex._determine_exit_reason = MagicMock(return_value="tp")
        ex._get_actual_exit_price = MagicMock(return_value=2060.0)
        ex._cancel_remaining_orders = MagicMock()
        ex.save_position = MagicMock()
        ex._append_trade_log = MagicMock()

        result = ex.check_position()
        assert result is not None
        assert result.exit_reason == ExitReason.TP
        ex.save_position.assert_called_with(None)

    def test_timeout_closes_position_then_cancels_orders(self):
        ex = make_executor()
        old_time = (datetime.now(timezone.utc) - timedelta(hours=100)).isoformat()
        pos = {
            "direction": "LONG",
            "entry_price": 2000,
            "size": "1",
            "sl_price": 1960,
            "tp_price": 2060,
            "sl_algo_id": "algo123",
            "tp_order_id": "ord456",
            "entry_time": old_time,
        }
        ex.load_position = MagicMock(return_value=pos)
        ex.client.get_positions.return_value = [{"pos": "1"}]  # Still open
        ex._emergency_close = MagicMock(return_value=True)
        ex._get_actual_exit_price = MagicMock(return_value=1990.0)
        ex.save_position = MagicMock()
        ex._append_trade_log = MagicMock()

        result = ex.check_position()
        assert result is not None
        assert result.exit_reason == ExitReason.TIMEOUT

        # Verify: close called BEFORE cancel
        ex._emergency_close.assert_called_once()
        # After close succeeds, orders should be cancelled
        ex.client.cancel_algo_order.assert_called_once()
        ex.client.cancel_order.assert_called_once()

    def test_timeout_close_fails_preserves_sl_tp(self):
        """If timeout close fails, SL/TP should NOT be cancelled."""
        ex = make_executor()
        old_time = (datetime.now(timezone.utc) - timedelta(hours=100)).isoformat()
        pos = {
            "direction": "LONG",
            "entry_price": 2000,
            "size": "1",
            "sl_algo_id": "algo123",
            "tp_order_id": "ord456",
            "entry_time": old_time,
        }
        ex.load_position = MagicMock(return_value=pos)
        ex.client.get_positions.return_value = [{"pos": "1"}]
        ex._emergency_close = MagicMock(return_value=False)  # Close failed!

        result = ex.check_position()
        assert result is None  # No result because close failed

        # CRITICAL: SL/TP should NOT be cancelled
        ex.client.cancel_algo_order.assert_not_called()
        ex.client.cancel_order.assert_not_called()


class TestEmergencyClose:
    @patch('time.sleep')  # speed up tests
    def test_already_closed(self, mock_sleep):
        ex = make_executor()
        ex.client.get_positions.return_value = []
        ex.save_position = MagicMock()
        assert ex._emergency_close("sell", "1") is True

    @patch('time.sleep')
    def test_closes_after_retry(self, mock_sleep):
        ex = make_executor()
        ex.client.get_positions.side_effect = [
            [{"pos": "1"}],         # before close
            [],                      # after close
        ]
        ex.client.place_market_order.return_value = {"code": "0"}
        ex.save_position = MagicMock()
        assert ex._emergency_close("sell", "1") is True

    @patch('time.sleep')
    def test_all_attempts_fail(self, mock_sleep):
        ex = make_executor()
        ex.client.get_positions.return_value = [{"pos": "1"}]
        ex.client.place_market_order.return_value = {"code": "1", "msg": "fail"}
        assert ex._emergency_close("sell", "1") is False


class TestReconcileAndStatus:
    def test_save_position_none_refuses_if_exchange_still_open(self, tmp_path):
        ex = make_executor()
        ex.client.get_positions.return_value = [{"pos": "-0.42"}]
        state_file = tmp_path / "position_state.json"
        with patch("okx_bb.executor.POSITION_STATE_FILE", state_file):
            ex.save_position(None)
            assert not state_file.exists()

    def test_reconcile_position_from_exchange_backfills_open_trade_log(self, tmp_path):
        ex = make_executor()
        ex.client.get_positions.return_value = [{
            "pos": "-0.42",
            "avgPx": "1963.4",
            "cTime": "1772595001158",
            "tradeId": "3525516747",
        }]
        ex.client.get_algo_orders.return_value = [{
            "algoId": "3370478016656039936",
            "side": "buy",
            "slTriggerPx": "2022.3",
        }]
        ex.client.get_open_orders.return_value = []
        state_file = tmp_path / "position_state.json"
        trade_log_file = tmp_path / "trade_log.json"

        with patch("okx_bb.executor.POSITION_STATE_FILE", state_file), \
             patch("okx_bb.executor.TRADE_LOG_FILE", trade_log_file):
            pos = ex.reconcile_position_from_exchange(source="test_reconcile")
            assert pos is not None
            assert pos["direction"] == "SHORT"
            assert pos["sl_algo_id"] == "3370478016656039936"

            state = json.loads(state_file.read_text())
            assert state["position"]["direction"] == "SHORT"

            log = json.loads(trade_log_file.read_text())
            open_rows = [x for x in log if x.get("status") == "OPEN"]
            assert len(open_rows) == 1
            assert open_rows[0]["entry_price"] == 1963.4
            assert open_rows[0]["direction"] == "SHORT"

    def test_position_status_handles_none_tp(self, tmp_path):
        ex = make_executor()
        pos_data = {
            "direction": "SHORT",
            "entry_price": 1963.4,
            "size": "0.42",
            "sl_price": 2022.3,
            "tp_price": None,
            "entry_time": "2026-03-04T03:30:01.158000+00:00",
        }
        state_file = tmp_path / "position_state.json"
        state_file.write_text(json.dumps({"position": pos_data}))
        ex.client.get_ticker.return_value = {"last": 1945.59}
        ex.client.get_balance.return_value = {"total_equity": 84.09}

        with patch("okx_bb.executor.POSITION_STATE_FILE", state_file):
            status = ex.position_status()
        assert "TP: None" in status
        assert "SHORT ETH @ $1963.40" in status


    def test_save_position_none_refuses_if_api_fails(self, tmp_path):
        """API failure should NOT clear local state."""
        ex = make_executor()
        ex.client.get_positions.return_value = None  # API error
        state_file = tmp_path / "position_state.json"
        state_file.write_text('{"position": {"direction": "SHORT"}}')
        with patch("okx_bb.executor.POSITION_STATE_FILE", state_file):
            ex.save_position(None)
            # File should still have old data, not be cleared
            data = json.loads(state_file.read_text())
            assert data["position"] is not None

    def test_save_position_none_succeeds_if_exchange_flat(self, tmp_path):
        """When exchange confirms flat, local should be cleared."""
        ex = make_executor()
        ex.client.get_positions.return_value = [{"pos": "0"}]
        state_file = tmp_path / "position_state.json"
        state_file.write_text('{"position": {"direction": "SHORT"}}')
        with patch("okx_bb.executor.POSITION_STATE_FILE", state_file):
            ex.save_position(None)
            data = json.loads(state_file.read_text())
            assert data["position"] is None

    def test_get_actual_exit_info_uses_fill_timestamp(self):
        """Exit time should come from exchange fill, not datetime.now()."""
        ex = make_executor()
        ex.client.get_fills.return_value = [{
            "fillPx": "1946.31",
            "ts": "1772979292595",  # 2026-03-08 14:14:52 UTC
        }]
        price, exit_time = ex._get_actual_exit_info({"tp_order_id": ""})
        assert price == 1946.31
        assert exit_time.year == 2026
        assert exit_time.month == 3
        assert exit_time.day == 8
        assert exit_time.hour == 14

    def test_get_actual_exit_info_fallback_to_ticker(self):
        """When no fills, fallback to ticker price with now() time."""
        ex = make_executor()
        ex.client.get_fills.return_value = []
        ex.client.get_ticker.return_value = {"last": 1950.0}
        price, exit_time = ex._get_actual_exit_info({"tp_order_id": ""})
        assert price == 1950.0

    def test_append_trade_log_dedup(self, tmp_path):
        """Same trade should not be logged twice."""
        ex = make_executor()
        trade_log_file = tmp_path / "trade_log.json"
        trade_log_file.write_text("[]")

        from core.types import TradeResult, Direction, ExitReason
        result = TradeResult(
            coin="ETH", direction=Direction.SHORT,
            entry_price=1963.4, exit_price=1946.31,
            size=0.42, pnl_pct=0.0077, pnl_usd=0.72,
            entry_time=datetime(2026, 3, 4, 3, 30, 1, tzinfo=timezone.utc),
            exit_time=datetime(2026, 3, 8, 14, 14, 52, tzinfo=timezone.utc),
            exit_reason=ExitReason.TIMEOUT,
            strategy="bb_breakout", fees_usd=0,
        )

        with patch("okx_bb.executor.TRADE_LOG_FILE", trade_log_file):
            ex._append_trade_log(result)
            ex._append_trade_log(result)  # second call should be deduped

        log = json.loads(trade_log_file.read_text())
        close_records = [r for r in log if r.get("exit_price") is not None]
        assert len(close_records) == 1

    def test_reconcile_returns_local_if_api_fails(self, tmp_path):
        """If API fails, reconcile should keep and return existing local state."""
        ex = make_executor()
        ex.client.get_positions.return_value = None  # API error
        state_file = tmp_path / "position_state.json"
        local_pos = {"direction": "SHORT", "entry_price": 1963.4}
        state_file.write_text(json.dumps({"position": local_pos}))

        with patch("okx_bb.executor.POSITION_STATE_FILE", state_file):
            result = ex.reconcile_position_from_exchange()
            assert result == local_pos

    def test_reconcile_clears_if_exchange_flat(self, tmp_path):
        """If exchange says flat, reconcile should clear local."""
        ex = make_executor()
        ex.client.get_positions.return_value = [{"pos": "0"}]
        state_file = tmp_path / "position_state.json"
        state_file.write_text(json.dumps({"position": {"direction": "SHORT"}}))

        with patch("okx_bb.executor.POSITION_STATE_FILE", state_file):
            result = ex.reconcile_position_from_exchange()
            assert result is None


class TestDetermineExitReason:
    def test_sl_triggered(self):
        ex = make_executor()
        pos = {"sl_algo_id": "algo1", "tp_order_id": "ord1",
               "sl_price": 1960, "tp_price": 2060}
        ex.client.get_algo_order_history.return_value = [
            {"algoId": "algo1", "state": "effective"}
        ]
        assert ex._determine_exit_reason(pos) == "sl"

    def test_tp_filled(self):
        ex = make_executor()
        pos = {"sl_algo_id": "algo1", "tp_order_id": "ord1",
               "sl_price": 1960, "tp_price": 2060}
        ex.client.get_algo_order_history.return_value = [
            {"algoId": "algo1", "state": "canceled"}
        ]
        ex.client.get_order_detail.return_value = {"state": "filled"}
        assert ex._determine_exit_reason(pos) == "tp"

    def test_unknown_fallback_to_fills(self):
        ex = make_executor()
        pos = {"sl_algo_id": "algo1", "tp_order_id": "ord1",
               "sl_price": 1960, "tp_price": 2060}
        ex.client.get_algo_order_history.return_value = []
        ex.client.get_order_detail.return_value = {"state": "live"}  # Not filled
        ex.client.get_fills.return_value = [{"fillPx": "1961"}]  # Close to SL
        assert ex._determine_exit_reason(pos) == "sl"

    def test_fills_fallback_tp_match(self):
        """Line 535: fills close to TP price → return 'tp'."""
        ex = make_executor()
        pos = {"sl_algo_id": "algo1", "tp_order_id": "ord1",
               "sl_price": 1960, "tp_price": 2060}
        ex.client.get_algo_order_history.return_value = []
        ex.client.get_order_detail.return_value = {"state": "live"}
        ex.client.get_fills.return_value = [{"fillPx": "2059"}]  # Close to TP
        assert ex._determine_exit_reason(pos) == "tp"

    def test_no_sl_algo_id(self):
        """When no sl_algo_id, skip algo history check."""
        ex = make_executor()
        pos = {"sl_algo_id": "", "tp_order_id": "ord1",
               "sl_price": 1960, "tp_price": 2060}
        ex.client.get_order_detail.return_value = {"state": "filled"}
        assert ex._determine_exit_reason(pos) == "tp"

    def test_unknown_when_no_fills(self):
        """No algo, no TP fill, no fills → unknown."""
        ex = make_executor()
        pos = {"sl_algo_id": "", "tp_order_id": "",
               "sl_price": 1960, "tp_price": 2060}
        ex.client.get_fills.return_value = []
        assert ex._determine_exit_reason(pos) == "unknown"

    def test_unknown_when_fill_price_zero(self):
        """Fill price is 0 → unknown."""
        ex = make_executor()
        pos = {"sl_algo_id": "", "tp_order_id": "",
               "sl_price": 1960, "tp_price": 2060}
        ex.client.get_fills.return_value = [{"fillPx": "0"}]
        assert ex._determine_exit_reason(pos) == "unknown"

    def test_unknown_when_fill_far_from_sl_tp(self):
        """Fill price not close to SL or TP → unknown."""
        ex = make_executor()
        pos = {"sl_algo_id": "", "tp_order_id": "",
               "sl_price": 1960, "tp_price": 2060}
        ex.client.get_fills.return_value = [{"fillPx": "2010"}]  # Far from both
        assert ex._determine_exit_reason(pos) == "unknown"


# ======================================================================
# Additional tests to raise coverage to 95%+
# ======================================================================

class TestCalculateSize:
    def test_max_loss_cap(self):
        """Line 229: max_loss > max_single_loss → cap notional."""
        ex = make_executor()
        # With equity=10000, position_ratio=0.30 → notional=3000
        # max_loss = 3000 * 0.02 = 60 > max_single_loss=10 → cap
        ex.client.get_balance.return_value = {"total_equity": 10000}
        ex.client.get_instrument.return_value = {"ctVal": "0.01", "lotSz": "0.01", "minSz": "0.01"}
        ex.client.get_ticker.return_value = {"last": 2000}
        result = ex.calculate_size()
        assert result is not None
        # notional should be capped: 10 / 0.02 = 500
        # contracts = 500 / (0.01 * 2000) = 25
        assert float(result) == 25.0

    def test_instrument_info_fails(self):
        """Lines 234-235: get_instrument returns None."""
        ex = make_executor()
        ex.client.get_balance.return_value = {"total_equity": 100}
        ex.client.get_instrument.return_value = None
        assert ex.calculate_size() is None

    def test_ticker_fails(self):
        """Lines 242-243: get_ticker returns None."""
        ex = make_executor()
        ex.client.get_balance.return_value = {"total_equity": 100}
        ex.client.get_instrument.return_value = {"ctVal": "0.01", "lotSz": "0.01", "minSz": "0.01"}
        ex.client.get_ticker.return_value = None
        assert ex.calculate_size() is None

    def test_contracts_below_minSz(self):
        """Line 251: contracts < minSz → use minSz."""
        ex = make_executor()
        ex.client.get_balance.return_value = {"total_equity": 1}  # Tiny equity
        ex.client.get_instrument.return_value = {"ctVal": "0.01", "lotSz": "0.01", "minSz": "5.0"}
        ex.client.get_ticker.return_value = {"last": 2000}
        result = ex.calculate_size()
        assert result is not None
        assert float(result) == 5.0


class TestCheckSignal:
    """Lines 189-208: check_signal method."""
    def test_insufficient_candles(self):
        ex = make_executor()
        ex.client.get_candles.return_value = [{"c": 100}] * 50  # < 120
        assert ex.check_signal() is None

    def test_signal_detected(self):
        ex = make_executor()
        candles = [{"c": float(1900 + i)} for i in range(300)]
        ex.client.get_candles.return_value = candles
        with patch("okx_bb.executor.detect_signal", return_value="LONG"):
            result = ex.check_signal()
        assert result == "LONG"

    def test_no_signal(self):
        ex = make_executor()
        candles = [{"c": float(1900 + i)} for i in range(300)]
        ex.client.get_candles.return_value = candles
        with patch("okx_bb.executor.detect_signal", return_value=None):
            result = ex.check_signal()
        assert result is None


class TestFetchAndCloses:
    """Lines 176, 180: fetch_candles and get_closes."""
    def test_fetch_candles(self):
        ex = make_executor()
        ex.client.get_candles.return_value = [{"c": 100}, {"c": 200}]
        result = ex.fetch_candles(limit=2)
        assert len(result) == 2

    def test_get_closes(self):
        ex = make_executor()
        candles = [{"c": 100.5}, {"c": 200.3}]
        closes = ex.get_closes(candles)
        assert closes == [100.5, 200.3]


class TestOpenPositionExtended:
    def test_empty_data_in_market_order(self):
        """Lines 296-298: market order returns empty data."""
        ex = make_executor()
        ex.client.get_positions.return_value = []
        ex.client.get_balance.return_value = {"total_equity": 100}
        ex.client.get_instrument.return_value = {"ctVal": "0.01"}
        ex.client.get_ticker.return_value = {"last": 2000}
        ex.client.place_market_order.return_value = {"code": "0", "data": []}
        assert ex.open_position("LONG") is False

    def test_no_ordId_in_response(self):
        """Lines 302-303: data present but no ordId."""
        ex = make_executor()
        ex.client.get_positions.return_value = []
        ex.client.get_balance.return_value = {"total_equity": 100}
        ex.client.get_instrument.return_value = {"ctVal": "0.01"}
        ex.client.get_ticker.return_value = {"last": 2000}
        ex.client.place_market_order.return_value = {"code": "0", "data": [{"ordId": ""}]}
        assert ex.open_position("LONG") is False

    def test_short_direction_sl_tp(self):
        """Lines 331-332: SHORT direction SL/TP calculation + full success."""
        ex = make_executor()
        ex.client.get_positions.return_value = []
        ex.client.get_balance.return_value = {"total_equity": 100}
        ex.client.get_instrument.return_value = {"ctVal": "0.01", "lotSz": "0.01", "minSz": "0.01"}
        ex.client.get_ticker.return_value = {"last": 2000}
        ex.client.place_market_order.return_value = {"code": "0", "data": [{"ordId": "ord1"}]}
        ex.client.get_order_detail.return_value = {"avgPx": "2000", "accFillSz": "1"}
        ex.client.place_stop_order.return_value = {"code": "0", "data": [{"algoId": "algo1"}]}
        ex.client.get_algo_orders.return_value = [{"algoId": "algo1", "slTriggerPx": "2040.00"}]
        ex.client.place_limit_order.return_value = {"code": "0", "data": [{"ordId": "tp1"}]}
        ex.save_position = MagicMock()

        result = ex.open_position("SHORT")
        assert result is True
        saved = ex.save_position.call_args[0][0]
        assert saved["direction"] == "SHORT"
        # SHORT: sl = entry * (1 + sl_pct), tp = entry * (1 - tp_pct)
        assert saved["sl_price"] == 2000 * 1.02
        assert saved["tp_price"] == 2000 * 0.97

    def test_tp_fails_but_position_kept(self):
        """Lines 357-360: TP order failure → warn but keep position with SL."""
        ex = make_executor()
        ex.client.get_positions.return_value = []
        ex.client.get_balance.return_value = {"total_equity": 100}
        ex.client.get_instrument.return_value = {"ctVal": "0.01", "lotSz": "0.01", "minSz": "0.01"}
        ex.client.get_ticker.return_value = {"last": 2000}
        ex.client.place_market_order.return_value = {"code": "0", "data": [{"ordId": "ord1"}]}
        ex.client.get_order_detail.return_value = {"avgPx": "2000", "accFillSz": "1"}
        ex.client.place_stop_order.return_value = {"code": "0", "data": [{"algoId": "algo1"}]}
        ex.client.get_algo_orders.return_value = [{"algoId": "algo1", "slTriggerPx": "1960.00"}]
        ex.client.place_limit_order.return_value = {"code": "1", "msg": "TP fail", "data": None}
        ex.save_position = MagicMock()

        result = ex.open_position("LONG")
        assert result is True
        saved = ex.save_position.call_args[0][0]
        assert saved["tp_order_id"] == ""  # TP failed, no order id

    def test_sl_fails_with_empty_data(self):
        """SL order returns code 0 but empty data."""
        ex = make_executor()
        ex.client.get_positions.return_value = []
        ex.client.get_balance.return_value = {"total_equity": 100}
        ex.client.get_instrument.return_value = {"ctVal": "0.01", "lotSz": "0.01", "minSz": "0.01"}
        ex.client.get_ticker.return_value = {"last": 2000}
        ex.client.place_market_order.return_value = {"code": "0", "data": [{"ordId": "ord1"}]}
        ex.client.get_order_detail.return_value = {"avgPx": "2000", "accFillSz": "1"}
        ex.client.place_stop_order.return_value = {"code": "0", "data": None}
        ex._emergency_close = MagicMock(return_value=True)

        assert ex.open_position("LONG") is False
        ex._emergency_close.assert_called_once()


class TestEmergencyCloseExtended:
    @patch('time.sleep')
    def test_api_error_then_close_succeeds(self, mock_sleep):
        """Line 406: positions API returns None but close still attempted."""
        ex = make_executor()
        ex.client.get_positions.side_effect = [
            None,                    # First check: API error
            [],                      # After close: verified flat
        ]
        ex.client.place_market_order.return_value = {"code": "0"}
        ex.save_position = MagicMock()
        assert ex._emergency_close("sell", "1") is True

    @patch('time.sleep')
    def test_close_succeeds_but_verify_still_has_position(self, mock_sleep):
        """Close order succeeds but verify shows still has position → retry."""
        ex = make_executor()
        ex.client.get_positions.side_effect = [
            [{"pos": "1"}],          # Attempt 1: still open
            [{"pos": "1"}],          # Attempt 1 verify: still open
            [{"pos": "1"}],          # Attempt 2: still open
            [],                      # Attempt 2 verify: closed
        ]
        ex.client.place_market_order.return_value = {"code": "0"}
        ex.save_position = MagicMock()
        assert ex._emergency_close("sell", "1") is True


class TestCancelRemainingOrders:
    """Lines 585-594: _cancel_remaining_orders with exception handling."""
    def test_cancel_succeeds(self):
        ex = make_executor()
        pos = {"sl_algo_id": "algo1", "tp_order_id": "ord1"}
        ex._cancel_remaining_orders(pos)
        ex.client.cancel_algo_order.assert_called_once_with("algo1", ex.instId)
        ex.client.cancel_order.assert_called_once_with(ex.instId, "ord1")

    def test_cancel_handles_exceptions(self):
        ex = make_executor()
        pos = {"sl_algo_id": "algo1", "tp_order_id": "ord1"}
        ex.client.cancel_algo_order.side_effect = Exception("already done")
        ex.client.cancel_order.side_effect = Exception("already done")
        # Should not raise
        ex._cancel_remaining_orders(pos)

    def test_cancel_skips_empty_ids(self):
        ex = make_executor()
        pos = {"sl_algo_id": "", "tp_order_id": ""}
        ex._cancel_remaining_orders(pos)
        ex.client.cancel_algo_order.assert_not_called()
        ex.client.cancel_order.assert_not_called()


class TestGetActualExitInfoExtended:
    def test_tp_order_detail_fallback(self):
        """Lines 615-619: no fills but TP order detail has fill."""
        ex = make_executor()
        ex.client.get_fills.return_value = []
        ex.client.get_order_detail.return_value = {
            "avgPx": "2060.5", "uTime": "1772979292595"
        }
        price, exit_time = ex._get_actual_exit_info({"tp_order_id": "ord1"})
        assert price == 2060.5
        assert exit_time.year == 2026

    def test_tp_order_detail_no_utime(self):
        """TP order detail found but uTime=0 → use now()."""
        ex = make_executor()
        ex.client.get_fills.return_value = []
        ex.client.get_order_detail.return_value = {"avgPx": "2060.5", "uTime": "0"}
        price, exit_time = ex._get_actual_exit_info({"tp_order_id": "ord1"})
        assert price == 2060.5

    def test_fallback_to_entry_price(self):
        """Line 626: no fills, no TP detail, no ticker → entry_price."""
        ex = make_executor()
        ex.client.get_fills.return_value = []
        ex.client.get_order_detail.return_value = None  # TP detail fails
        ex.client.get_ticker.return_value = None  # Ticker fails
        price, exit_time = ex._get_actual_exit_info(
            {"tp_order_id": "ord1", "entry_price": 1950.0}
        )
        assert price == 1950.0

    def test_no_tp_order_id_falls_to_ticker(self):
        """No tp_order_id → skip TP check → use ticker."""
        ex = make_executor()
        ex.client.get_fills.return_value = []
        ex.client.get_ticker.return_value = {"last": 1999.0}
        price, _ = ex._get_actual_exit_info({"tp_order_id": "", "entry_price": 1950.0})
        assert price == 1999.0


class TestRecordClosedPosition:
    def test_short_pnl_calculation(self):
        """Line 639: SHORT pnl = (entry - exit) / entry."""
        ex = make_executor()
        pos = {
            "direction": "SHORT",
            "entry_price": 2000,
            "size": "1",
            "entry_time": "2026-01-01T00:00:00+00:00",
            "sl_price": 2040,
            "tp_price": 1940,
            "tp_order_id": "",
        }
        ex.client.get_fills.return_value = [{"fillPx": "1950", "ts": "1772979292595"}]
        ex._append_trade_log = MagicMock()

        result = ex._record_closed_position(pos, "tp")
        # PnL = (2000 - 1950) / 2000 = 0.025, minus fees
        assert result.pnl_pct > 0
        assert result.exit_reason == ExitReason.TP
        assert result.direction.value == "SHORT"

    def test_unknown_reason_maps_to_timeout(self):
        """Unknown exit reason maps to TIMEOUT."""
        ex = make_executor()
        pos = {
            "direction": "LONG",
            "entry_price": 2000,
            "size": "1",
            "entry_time": "2026-01-01T00:00:00+00:00",
            "tp_order_id": "",
        }
        ex.client.get_fills.return_value = [{"fillPx": "1990", "ts": "1772979292595"}]
        ex._append_trade_log = MagicMock()

        result = ex._record_closed_position(pos, "unknown")
        assert result.exit_reason == ExitReason.TIMEOUT

    def test_sl_reason(self):
        ex = make_executor()
        pos = {
            "direction": "LONG",
            "entry_price": 2000,
            "size": "1",
            "entry_time": "2026-01-01T00:00:00+00:00",
            "tp_order_id": "",
        }
        ex.client.get_fills.return_value = [{"fillPx": "1960", "ts": "1772979292595"}]
        ex._append_trade_log = MagicMock()

        result = ex._record_closed_position(pos, "sl")
        assert result.exit_reason == ExitReason.SL


class TestAppendTradeLogExtended:
    def test_marks_open_records_as_closed(self, tmp_path):
        """Lines 683-684, 701-704: OPEN records get marked CLOSED."""
        ex = make_executor()
        trade_log_file = tmp_path / "trade_log.json"
        open_record = {
            "direction": "LONG",
            "entry_time": "2026-01-01T00:00:00+00:00",
            "status": "OPEN",
            "entry_price": 2000,
            "size": 1,
        }
        trade_log_file.write_text(json.dumps([open_record]))

        from core.types import TradeResult, Direction, ExitReason
        result = TradeResult(
            coin="ETH", direction=Direction.LONG,
            entry_price=2000, exit_price=2060,
            size=1, pnl_pct=0.03, pnl_usd=0,
            entry_time=datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
            exit_time=datetime(2026, 1, 2, 0, 0, 0, tzinfo=timezone.utc),
            exit_reason=ExitReason.TP,
            strategy="bb_breakout", fees_usd=0,
        )

        with patch("okx_bb.executor.TRADE_LOG_FILE", trade_log_file):
            ex._append_trade_log(result)

        log = json.loads(trade_log_file.read_text())
        # The OPEN record should now be CLOSED
        assert log[0]["status"] == "CLOSED"
        # And there should be a new close record
        assert len(log) == 2
        assert log[1]["exit_price"] == 2060

    def test_corrupt_log_file(self, tmp_path):
        """Lines 683-684: corrupt JSON in trade log → graceful fallback."""
        ex = make_executor()
        trade_log_file = tmp_path / "trade_log.json"
        trade_log_file.write_text("NOT VALID JSON")

        from core.types import TradeResult, Direction, ExitReason
        result = TradeResult(
            coin="ETH", direction=Direction.LONG,
            entry_price=2000, exit_price=2060,
            size=1, pnl_pct=0.03, pnl_usd=0,
            entry_time=datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
            exit_time=datetime(2026, 1, 2, 0, 0, 0, tzinfo=timezone.utc),
            exit_reason=ExitReason.TP,
            strategy="bb_breakout", fees_usd=0,
        )

        with patch("okx_bb.executor.TRADE_LOG_FILE", trade_log_file):
            ex._append_trade_log(result)

        log = json.loads(trade_log_file.read_text())
        assert len(log) == 1


class TestPositionStatusExtended:
    def test_no_position_returns_string(self):
        """Line 551: no position → 'No position'."""
        ex = make_executor()
        ex.load_position = MagicMock(return_value=None)
        assert ex.position_status() == "No position"

    def test_position_status_with_pos_param(self):
        """Line 551: position_status(pos=...) uses provided pos."""
        ex = make_executor()
        pos = {
            "direction": "LONG",
            "entry_price": 2000,
            "size": "10",
            "sl_price": 1960,
            "tp_price": 2060,
            "entry_time": "2026-01-01T00:00:00+00:00",
        }
        ex.client.get_ticker.return_value = {"last": 2050}
        ex.client.get_balance.return_value = {"total_equity": 100}
        status = ex.position_status(pos=pos)
        assert "LONG ETH @ $2000.00" in status
        assert "$1960.00" in status

    def test_short_pnl(self):
        """Line 560: SHORT pnl_usd = (entry - current) * size_coin."""
        ex = make_executor()
        pos = {
            "direction": "SHORT",
            "entry_price": 2000,
            "size": "10",
            "sl_price": 2040,
            "tp_price": 1940,
            "entry_time": "2026-01-01T00:00:00+00:00",
        }
        ex.client.get_ticker.return_value = {"last": 1950}
        ex.client.get_balance.return_value = {"total_equity": 100}
        status = ex.position_status(pos=pos)
        assert "SHORT ETH" in status
        # PnL should be positive (entry > current for SHORT)
        assert "+$" in status or "$+" in status or "+0" in status

    def test_zero_equity(self):
        """Line 570: pnl_pct = 0 when equity <= 0."""
        ex = make_executor()
        pos = {
            "direction": "LONG",
            "entry_price": 2000,
            "size": "10",
            "sl_price": 1960,
            "tp_price": 2060,
            "entry_time": "2026-01-01T00:00:00+00:00",
        }
        ex.client.get_ticker.return_value = {"last": 2050}
        ex.client.get_balance.return_value = {"total_equity": 0}
        status = ex.position_status(pos=pos)
        assert "+0.00% of account" in status


class TestRunOnce:
    """Lines 701-704, 734-751: run_once method."""
    def test_position_closed_returns_result(self):
        """Lines 734-736: check_position returns a result."""
        ex = make_executor()
        from core.types import TradeResult, Direction, ExitReason
        mock_result = TradeResult(
            coin="ETH", direction=Direction.LONG,
            entry_price=2000, exit_price=2060,
            size=1, pnl_pct=0.03, pnl_usd=0,
            entry_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
            exit_time=datetime(2026, 1, 2, tzinfo=timezone.utc),
            exit_reason=ExitReason.TP,
            strategy="bb_breakout", fees_usd=0,
        )
        ex.check_position = MagicMock(return_value=mock_result)
        status = ex.run_once()
        assert "Position closed" in status
        assert "TP" in status

    def test_still_in_position(self):
        """Lines 738-741: still in position → return status."""
        ex = make_executor()
        ex.check_position = MagicMock(return_value=None)
        ex.load_position = MagicMock(return_value={
            "direction": "LONG", "entry_price": 2000, "size": "1",
            "sl_price": 1960, "tp_price": 2060,
            "entry_time": "2026-01-01T00:00:00+00:00",
        })
        ex.client.get_ticker.return_value = {"last": 2010}
        ex.client.get_balance.return_value = {"total_equity": 100}
        status = ex.run_once()
        assert "LONG ETH" in status

    def test_new_signal_opens_position(self):
        """Lines 744-748: signal detected → open position."""
        ex = make_executor()
        ex.check_position = MagicMock(return_value=None)
        ex.load_position = MagicMock(return_value=None)
        ex.check_signal = MagicMock(return_value="LONG")
        ex.open_position = MagicMock(return_value=True)
        status = ex.run_once()
        assert "Opened LONG" in status

    def test_signal_but_open_fails(self):
        """Line 749: signal detected but open fails."""
        ex = make_executor()
        ex.check_position = MagicMock(return_value=None)
        ex.load_position = MagicMock(return_value=None)
        ex.check_signal = MagicMock(return_value="SHORT")
        ex.open_position = MagicMock(return_value=False)
        status = ex.run_once()
        assert "Signal SHORT but open failed" in status

    def test_no_signal(self):
        """Line 751: no signal → 'No signal'."""
        ex = make_executor()
        ex.check_position = MagicMock(return_value=None)
        ex.load_position = MagicMock(return_value=None)
        ex.check_signal = MagicMock(return_value=None)
        status = ex.run_once()
        assert status == "No signal"


class TestAppendOpenTradeLogIfMissing:
    """Lines 77-82, 93-94: _append_open_trade_log_if_missing."""
    def test_creates_new_open_record(self, tmp_path):
        ex = make_executor()
        trade_log_file = tmp_path / "trade_log.json"
        with patch("okx_bb.executor.TRADE_LOG_FILE", trade_log_file):
            pos = {"direction": "LONG", "entry_price": 2000, "size": "1",
                   "entry_time": "2026-01-01T00:00:00+00:00",
                   "sl_price": 1960, "tp_price": 2060}
            ex._append_open_trade_log_if_missing(pos, exchange_trade_id="t1", source="test")
        log = json.loads(trade_log_file.read_text())
        assert len(log) == 1
        assert log[0]["status"] == "OPEN"
        assert log[0]["source"] == "test"

    def test_updates_existing_open_record(self, tmp_path):
        """Lines 93-94: existing OPEN record → update last_sync fields."""
        ex = make_executor()
        trade_log_file = tmp_path / "trade_log.json"
        existing = [{
            "status": "OPEN", "direction": "LONG",
            "entry_price": 2000, "size": 1,
        }]
        trade_log_file.write_text(json.dumps(existing))
        with patch("okx_bb.executor.TRADE_LOG_FILE", trade_log_file):
            pos = {"direction": "LONG", "entry_price": 2000, "size": "1"}
            ex._append_open_trade_log_if_missing(pos, source="resync")
        log = json.loads(trade_log_file.read_text())
        assert len(log) == 1  # No new record added
        assert log[0]["last_sync_source"] == "resync"

    def test_corrupt_trade_log(self, tmp_path):
        """Lines 81-82: corrupt JSON → log error, start fresh."""
        ex = make_executor()
        trade_log_file = tmp_path / "trade_log.json"
        trade_log_file.write_text("CORRUPT")
        with patch("okx_bb.executor.TRADE_LOG_FILE", trade_log_file):
            pos = {"direction": "LONG", "entry_price": 2000, "size": "1",
                   "entry_time": "2026-01-01T00:00:00+00:00",
                   "sl_price": 1960, "tp_price": 2060}
            ex._append_open_trade_log_if_missing(pos, source="test")
        log = json.loads(trade_log_file.read_text())
        assert len(log) == 1

    def test_log_file_not_list(self, tmp_path):
        """Line 79: JSON valid but not a list → start with empty list."""
        ex = make_executor()
        trade_log_file = tmp_path / "trade_log.json"
        trade_log_file.write_text('{"key": "value"}')
        with patch("okx_bb.executor.TRADE_LOG_FILE", trade_log_file):
            pos = {"direction": "LONG", "entry_price": 2000, "size": "1",
                   "entry_time": "2026-01-01T00:00:00+00:00",
                   "sl_price": 1960, "tp_price": 2060}
            ex._append_open_trade_log_if_missing(pos, source="test")
        log = json.loads(trade_log_file.read_text())
        assert len(log) == 1


class TestReconcileExtended:
    def test_reconcile_long_position_with_tp(self, tmp_path):
        """Reconcile a LONG position with TP order."""
        ex = make_executor()
        ex.client.get_positions.return_value = [{
            "pos": "0.42",
            "avgPx": "1963.4",
            "cTime": "1772595001158",
            "tradeId": "123",
        }]
        ex.client.get_algo_orders.return_value = []
        ex.client.get_open_orders.return_value = [{
            "side": "sell", "reduceOnly": "true",
            "px": "2020.0", "ordId": "tp_ord_1",
        }]
        trade_log_file = tmp_path / "trade_log.json"
        with patch("okx_bb.executor.TRADE_LOG_FILE", trade_log_file):
            pos = ex.reconcile_position_from_exchange()
        assert pos["direction"] == "LONG"
        assert pos["tp_price"] == 2020.0
        assert pos["tp_order_id"] == "tp_ord_1"

    def test_reconcile_no_ctime(self, tmp_path):
        """cTime missing → use datetime.now()."""
        ex = make_executor()
        ex.client.get_positions.return_value = [{
            "pos": "0.42",
            "avgPx": "1963.4",
        }]
        ex.client.get_algo_orders.return_value = []
        ex.client.get_open_orders.return_value = []
        trade_log_file = tmp_path / "trade_log.json"
        with patch("okx_bb.executor.TRADE_LOG_FILE", trade_log_file):
            pos = ex.reconcile_position_from_exchange()
        assert pos is not None
        assert pos["direction"] == "LONG"


class TestCheckPositionExtended:
    def test_not_timed_out_returns_none(self):
        """Line 497: position exists, not timed out → return None."""
        ex = make_executor()
        recent_time = datetime.now(timezone.utc).isoformat()
        pos = {
            "direction": "LONG",
            "entry_price": 2000,
            "size": "1",
            "sl_price": 1960,
            "tp_price": 2060,
            "entry_time": recent_time,
        }
        ex.load_position = MagicMock(return_value=pos)
        ex.client.get_positions.return_value = [{"pos": "1"}]
        result = ex.check_position()
        assert result is None


class TestMain:
    """Lines 734-785: main() function."""
    @patch("okx_bb.executor.BBExecutor")
    @patch("sys.argv", ["executor", "--status"])
    def test_main_status(self, MockExecutor, capsys):
        from okx_bb.executor import main
        mock_ex = MagicMock()
        mock_ex.position_status.return_value = "No position"
        mock_ex.client.get_balance.return_value = {"total_equity": 100.0}
        MockExecutor.return_value = mock_ex
        main()
        out = capsys.readouterr().out
        assert "No position" in out
        assert "$100.00" in out

    @patch("okx_bb.executor.BBExecutor")
    @patch("sys.argv", ["executor", "--dry-run"])
    def test_main_dry_run(self, MockExecutor, capsys):
        from okx_bb.executor import main
        mock_ex = MagicMock()
        mock_ex.check_signal.return_value = "LONG"
        MockExecutor.return_value = mock_ex
        main()
        out = capsys.readouterr().out
        assert "Signal: LONG" in out

    @patch("okx_bb.executor.BBExecutor")
    @patch("sys.argv", ["executor"])
    def test_main_run_once(self, MockExecutor, capsys):
        from okx_bb.executor import main
        mock_ex = MagicMock()
        mock_ex.run_once.return_value = "No signal"
        MockExecutor.return_value = mock_ex
        main()
        out = capsys.readouterr().out
        assert "No signal" in out
