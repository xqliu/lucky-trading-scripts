#!/usr/bin/env python3
"""Tests for reconcile_orphan_positions()"""
import json
import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path


@pytest.fixture(autouse=True)
def mock_workspace(tmp_path, monkeypatch):
    state_dir = tmp_path / "memory" / "trading"
    state_dir.mkdir(parents=True)
    # No position in state
    for coin in ["BTC", "ETH"]:
        sf = state_dir / f"position_state_{coin}.json"
        sf.write_text(json.dumps({"position": None}))
    monkeypatch.setattr("luckytrader.execute._WORKSPACE_DIR", tmp_path)
    monkeypatch.setattr("luckytrader.execute.STATE_FILE", state_dir / "position_state.json")


@patch("luckytrader.execute.notify_discord")
@patch("luckytrader.execute.place_take_profit", return_value={"status": "ok"})
@patch("luckytrader.execute.place_stop_loss", return_value={"status": "ok"})
@patch("luckytrader.execute.check_sl_tp_orders", return_value=(False, False))
@patch("luckytrader.execute.get_position")
def test_orphan_detected_and_fixed(mock_pos, mock_check, mock_sl, mock_tp, mock_notify):
    """Chain has SHORT but state is empty → should reconcile."""
    # BTC: no position on chain. ETH: orphan SHORT
    def fake_pos(coin):
        if coin == "ETH":
            return {"direction": "SHORT", "entryPx": "2035.4", "size": -0.0093}
        return None
    mock_pos.side_effect = fake_pos
    
    from luckytrader.execute import reconcile_orphan_positions
    result = reconcile_orphan_positions()
    
    assert len(result) == 1
    assert result[0]["coin"] == "ETH"
    assert result[0]["direction"] == "SHORT"
    
    # SL and TP should be set
    mock_sl.assert_called_once()
    mock_tp.assert_called_once()
    
    # Discord notification
    mock_notify.assert_called_once()
    assert "孤儿仓位" in mock_notify.call_args[0][0]


@patch("luckytrader.execute.notify_discord")
@patch("luckytrader.execute.get_position", return_value=None)
def test_no_orphans(mock_pos, mock_notify):
    """No chain positions, no state → nothing to do."""
    from luckytrader.execute import reconcile_orphan_positions
    result = reconcile_orphan_positions()
    assert result == []
    mock_notify.assert_not_called()


@patch("luckytrader.execute.notify_discord")
@patch("luckytrader.execute.get_position", return_value=None)
def test_stale_state_cleaned(mock_pos, mock_notify, tmp_path, monkeypatch):
    """State has position but chain empty → clean up state."""
    # Write a stale state to the main STATE_FILE
    import luckytrader.execute as exe
    sf = exe.STATE_FILE
    sf.write_text(json.dumps({
        "BTC": {"position": {"coin": "BTC", "direction": "LONG", "entry_price": 65000}},
    }))
    
    from luckytrader.execute import reconcile_orphan_positions, load_state
    result = reconcile_orphan_positions()
    
    # No orphans returned (stale cleanup is silent)
    assert result == []
    # State should be cleaned for BTC
    state = load_state("BTC")
    assert state.get("position") is None
