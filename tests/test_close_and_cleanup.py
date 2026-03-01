#!/usr/bin/env python3
"""Tests for close_and_cleanup() — the unified close function."""
import json
import pytest
from unittest.mock import patch, MagicMock, call
from pathlib import Path


@pytest.fixture(autouse=True)
def mock_workspace(tmp_path, monkeypatch):
    """Set up workspace so state files don't conflict."""
    state_dir = tmp_path / "memory" / "trading"
    state_dir.mkdir(parents=True)
    state_file = state_dir / "position_state.json"
    state_file.write_text(json.dumps({
        "position": {"coin": "BTC", "entry_price": 65000, "size": 0.001,
                      "direction": "SHORT", "regime": "trend"}
    }))
    monkeypatch.setattr("luckytrader.execute._WORKSPACE_DIR", tmp_path)
    monkeypatch.setattr("luckytrader.execute.STATE_FILE", state_file)


@patch("luckytrader.execute.notify_discord")
@patch("luckytrader.execute.log_trade")
@patch("luckytrader.execute.record_trade_result")
@patch("luckytrader.execute.cancel_order")
@patch("luckytrader.execute.get_open_orders_detailed", return_value=[
    {"coin": "BTC", "isTrigger": True, "oid": 123},
    {"coin": "BTC", "isTrigger": True, "oid": 456},
])
@patch("luckytrader.execute.get_market_price", return_value=64000.0)
@patch("luckytrader.execute.place_market_order", return_value={"status": "ok"})
def test_close_and_cleanup_basic(mock_order, mock_price, mock_orders, mock_cancel,
                                  mock_record, mock_log, mock_notify):
    from luckytrader.execute import close_and_cleanup
    
    result = close_and_cleanup(
        coin="BTC", is_long=False, size=0.001,
        reason="EARLY_EXIT", pnl_pct=1.5,
        extra_msg="MFE too low"
    )
    
    # Market order placed (buy to close short)
    mock_order.assert_called_once_with("BTC", True, 0.001)
    
    # Orders cancelled
    assert mock_cancel.call_count == 2
    
    # Trade recorded
    mock_record.assert_called_once_with(1.5, "SHORT", "BTC", "EARLY_EXIT")
    
    # Log written
    mock_log.assert_called_once()
    
    # Discord notified
    mock_notify.assert_called_once()
    assert "EARLY_EXIT" in mock_notify.call_args[0][0]
    assert "MFE too low" in mock_notify.call_args[0][0]
    
    # Return value
    assert result["close_price"] == 64000.0
    assert result["pnl_pct"] == 1.5
    assert result["reason"] == "EARLY_EXIT"


@patch("luckytrader.execute.notify_discord")
@patch("luckytrader.execute.log_trade")
@patch("luckytrader.execute.record_trade_result")
@patch("luckytrader.execute.get_open_orders_detailed", return_value=[])
@patch("luckytrader.execute.get_market_price", return_value=66000.0)
@patch("luckytrader.execute.place_market_order", return_value={"status": "ok"})
def test_close_and_cleanup_auto_pnl(mock_order, mock_price, mock_orders,
                                     mock_record, mock_log, mock_notify):
    """When pnl_pct is None, it should compute from state."""
    from luckytrader.execute import close_and_cleanup
    
    result = close_and_cleanup(
        coin="BTC", is_long=False, size=0.001,
        reason="REGIME_TP"
    )
    
    # PnL auto-computed: SHORT from 65000, close at 66000 = (65000-66000)/65000 = -1.538%
    assert result["pnl_pct"] < 0, f"SHORT close at higher price should lose money, got {result['pnl_pct']}"


@patch("luckytrader.execute.place_market_order", return_value={"status": "err", "msg": "insufficient"})
def test_close_and_cleanup_order_failure(mock_order):
    """Should raise if market order fails."""
    from luckytrader.execute import close_and_cleanup
    
    with pytest.raises(Exception, match="close_and_cleanup order error"):
        close_and_cleanup("BTC", False, 0.001, "TEST")


@patch("luckytrader.execute.notify_discord")
@patch("luckytrader.execute.log_trade")
@patch("luckytrader.execute.record_trade_result")
@patch("luckytrader.execute.cancel_order", side_effect=Exception("API timeout"))
@patch("luckytrader.execute.get_open_orders_detailed", return_value=[
    {"coin": "BTC", "isTrigger": True, "oid": 789},
])
@patch("luckytrader.execute.get_market_price", return_value=64000.0)
@patch("luckytrader.execute.place_market_order", return_value={"status": "ok"})
def test_close_and_cleanup_cancel_failure_nonfatal(mock_order, mock_price, mock_orders,
                                                    mock_cancel, mock_record, mock_log, mock_notify):
    """Cancel order failure should NOT prevent the rest of cleanup."""
    from luckytrader.execute import close_and_cleanup
    
    # Should not raise
    result = close_and_cleanup("BTC", False, 0.001, "EARLY_EXIT", pnl_pct=0.5)
    assert result["reason"] == "EARLY_EXIT"
    # Trade still recorded despite cancel failure
    mock_record.assert_called_once()
