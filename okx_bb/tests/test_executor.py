"""Tests for BBExecutor — mock OKXClient to test execution logic."""
import sys
import json
from pathlib import Path
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# Mock send_discord BEFORE importing executor to prevent real Discord messages
import unittest.mock
_discord_mock = unittest.mock.patch('core.notify.send_discord', return_value=True)
_discord_mock.start()

from okx_bb.executor import BBExecutor, POSITION_STATE_FILE
from okx_bb.config import OKXConfig, StrategyConfig, RiskConfig, FeeConfig
from core.types import ExitReason


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
