"""Regression tests for trade_log.json field completeness.

Bug: _append_trade_log was missing size, pnl_usd, fees_usd fields.
Fix: dbbeecf
"""
import json
import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path
from datetime import datetime, timezone
from core.types import TradeResult, Direction, ExitReason


@pytest.fixture
def tmp_state_dir(tmp_path):
    """Redirect STATE_DIR to temp."""
    return tmp_path


def _make_trade_result(**overrides):
    defaults = dict(
        coin="ETH",
        direction=Direction.LONG,
        entry_price=2000.0,
        exit_price=2050.0,
        size=0.41,
        pnl_pct=2.5,
        pnl_usd=2.05,
        fees_usd=0.08,
        entry_time=datetime(2026, 3, 20, 10, 0, tzinfo=timezone.utc),
        exit_time=datetime(2026, 3, 20, 14, 0, tzinfo=timezone.utc),
        exit_reason=ExitReason.TP,
    )
    defaults.update(overrides)
    return TradeResult(**defaults)


class TestETHTradeLogFields:
    """ETH executor trade log must include size, pnl_usd, fees_usd."""

    def test_trade_log_contains_size(self, tmp_state_dir):
        with patch("okx_bb.executor.STATE_DIR", tmp_state_dir), \
             patch("okx_bb.executor.TRADE_LOG_FILE", tmp_state_dir / "trade_log.json"):
            from okx_bb.executor import BBExecutor
            ex = BBExecutor.__new__(BBExecutor)
            ex.instId = "ETH-USDT-SWAP"
            ex.cfg = MagicMock()
            ex.cfg.coin = "ETH"

            result = _make_trade_result()
            ex._append_trade_log(result)

            log = json.loads((tmp_state_dir / "trade_log.json").read_text())
            assert len(log) == 1
            entry = log[0]
            assert entry["size"] == 0.41, "size field must be present"
            assert entry["pnl_usd"] == 2.05, "pnl_usd field must be present"
            assert entry["fees_usd"] == 0.08, "fees_usd field must be present"
            assert entry["exit_price"] == 2050.0
            assert entry["entry_price"] == 2000.0

    def test_trade_log_no_missing_fields(self, tmp_state_dir):
        """All required fields must be present in every log entry."""
        required = {"coin", "direction", "entry_price", "exit_price", "size",
                     "pnl_pct", "pnl_usd", "fees_usd", "entry_time", "exit_time", "exit_reason"}
        with patch("okx_bb.executor.STATE_DIR", tmp_state_dir), \
             patch("okx_bb.executor.TRADE_LOG_FILE", tmp_state_dir / "trade_log.json"):
            from okx_bb.executor import BBExecutor
            ex = BBExecutor.__new__(BBExecutor)
            ex.instId = "ETH-USDT-SWAP"
            ex.cfg = MagicMock()
            ex.cfg.coin = "ETH"

            ex._append_trade_log(_make_trade_result())
            log = json.loads((tmp_state_dir / "trade_log.json").read_text())
            missing = required - set(log[0].keys())
            assert not missing, f"Missing fields: {missing}"


class TestSOLTradeLogFields:
    """SOL executor trade log must include size, pnl_usd, fees_usd."""

    def test_trade_log_contains_size(self, tmp_state_dir):
        with patch("okx_sol_bb.executor.STATE_DIR", tmp_state_dir), \
             patch("okx_sol_bb.executor.TRADE_LOG_FILE", tmp_state_dir / "trade_log.json"):
            from okx_sol_bb.executor import SolBBExecutor
            ex = SolBBExecutor.__new__(SolBBExecutor)
            ex.instId = "SOL-USDT-SWAP"
            ex.cfg = MagicMock()
            ex.cfg.coin = "SOL"

            result = _make_trade_result(coin="SOL")
            ex._append_trade_log(result)

            log = json.loads((tmp_state_dir / "trade_log.json").read_text())
            entry = log[0]
            assert entry["size"] == 0.41
            assert entry["pnl_usd"] == 2.05
            assert entry["fees_usd"] == 0.08


class TestGetFillsHistory:
    """OKXClient.get_fills_history and get_all_fills_history."""

    def test_get_fills_history_calls_correct_endpoint(self):
        from okx_bb.exchange import OKXClient
        client = OKXClient.__new__(OKXClient)
        client._request = MagicMock(return_value={"code": "0", "data": [{"billId": "1"}]})

        result = client.get_fills_history(instId="ETH-USDT-SWAP")
        assert result == [{"billId": "1"}]
        args = client._request.call_args
        assert args[0][0] == "GET"
        assert args[0][1] == "/trade/fills-history"
        params = args[1].get("params") or args[0][2]
        assert params["instType"] == "SWAP"
        assert params["instId"] == "ETH-USDT-SWAP"

    def test_get_fills_history_pagination(self):
        from okx_bb.exchange import OKXClient
        client = OKXClient.__new__(OKXClient)
        client._request = MagicMock(return_value={"code": "0", "data": [{"billId": "1"}]})

        result = client.get_fills_history(instId="ETH-USDT-SWAP", before="999")
        params = client._request.call_args[1].get("params") or client._request.call_args[0][2]
        assert params["before"] == "999"

    def test_get_all_fills_history_paginates(self):
        from okx_bb.exchange import OKXClient
        client = OKXClient.__new__(OKXClient)
        # First call returns 100 items (triggers pagination), second returns 1 (stops)
        page1 = [{"billId": str(i)} for i in range(100, 0, -1)]
        page2 = [{"billId": "0"}]
        client.get_fills_history = MagicMock(side_effect=[page1, page2])

        result = client.get_all_fills_history(instId="ETH-USDT-SWAP")
        assert len(result) == 101
        assert client.get_fills_history.call_count == 2
        # Second call should use before="1" (last billId of page1)
        assert client.get_fills_history.call_args_list[1][1].get("before") == "1"

    def test_get_fills_default_limit_100(self):
        from okx_bb.exchange import OKXClient
        client = OKXClient.__new__(OKXClient)
        client._request = MagicMock(return_value={"code": "0", "data": []})

        client.get_fills(instId="ETH-USDT-SWAP")
        params = client._request.call_args[1].get("params") or client._request.call_args[0][2]
        assert params["limit"] == "100", "Default limit should be 100, not 10"
