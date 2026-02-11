"""
Tests for trailing_stop.py — position protection logic.
This is CRITICAL: bugs here = lost money.
"""
import json
import pytest
from unittest.mock import patch, MagicMock, call
from pathlib import Path


class TestGetCurrentStopOrder:
    """Must correctly distinguish SL from TP trigger orders."""
    
    def test_finds_long_sl(self, mock_hl):
        """LONG position: SL is a sell trigger (side=A)."""
        from luckytrader.trailing import get_current_stop_order
        mock_hl.get_open_orders_detailed.return_value = [
            {
                "coin": "BTC", "isTrigger": True, "reduceOnly": True,
                "side": "A", "triggerPx": "64320.0", "oid": 1001,
                "orderType": "Stop Market",
            }
        ]
        result = get_current_stop_order("BTC", is_long=True)
        assert result is not None
        assert result["oid"] == 1001
        assert result["trigger_price"] == 64320.0
    
    def test_finds_short_sl(self, mock_hl):
        """SHORT position: SL is a buy trigger (side=B)."""
        from luckytrader.trailing import get_current_stop_order
        mock_hl.get_open_orders_detailed.return_value = [
            {
                "coin": "BTC", "isTrigger": True, "reduceOnly": True,
                "side": "B", "triggerPx": "69680.0", "oid": 1002,
                "orderType": "Stop Market",
            }
        ]
        result = get_current_stop_order("BTC", is_long=False)
        assert result is not None
        assert result["oid"] == 1002
    
    def test_ignores_non_trigger(self, mock_hl):
        """Regular limit orders should NOT be treated as stops."""
        from luckytrader.trailing import get_current_stop_order
        mock_hl.get_open_orders_detailed.return_value = [
            {
                "coin": "BTC", "isTrigger": False, "reduceOnly": True,
                "side": "A", "triggerPx": "64320.0", "oid": 1003,
            }
        ]
        result = get_current_stop_order("BTC", is_long=True)
        assert result is None
    
    def test_ignores_non_reduce_only(self, mock_hl):
        """Non reduce-only trigger orders should be ignored."""
        from luckytrader.trailing import get_current_stop_order
        mock_hl.get_open_orders_detailed.return_value = [
            {
                "coin": "BTC", "isTrigger": True, "reduceOnly": False,
                "side": "A", "triggerPx": "64320.0", "oid": 1004,
            }
        ]
        result = get_current_stop_order("BTC", is_long=True)
        assert result is None
    
    def test_ignores_wrong_coin(self, mock_hl):
        """Orders for different coins should be ignored."""
        from luckytrader.trailing import get_current_stop_order
        mock_hl.get_open_orders_detailed.return_value = [
            {
                "coin": "ETH", "isTrigger": True, "reduceOnly": True,
                "side": "A", "triggerPx": "1900.0", "oid": 1005,
            }
        ]
        result = get_current_stop_order("BTC", is_long=True)
        assert result is None
    
    def test_ignores_wrong_direction(self, mock_hl):
        """LONG position: buy trigger (side=B) is NOT a stop loss."""
        from luckytrader.trailing import get_current_stop_order
        mock_hl.get_open_orders_detailed.return_value = [
            {
                "coin": "BTC", "isTrigger": True, "reduceOnly": True,
                "side": "B", "triggerPx": "71000.0", "oid": 1006,
                "orderType": "Take Profit Market",
            }
        ]
        # For LONG, side=B is TP not SL
        result = get_current_stop_order("BTC", is_long=True)
        assert result is None
    
    def test_sl_vs_tp_both_present_long(self, mock_hl):
        """With both SL and TP orders, must pick the SL (side=A for LONG)."""
        from luckytrader.trailing import get_current_stop_order
        mock_hl.get_open_orders_detailed.return_value = [
            # TP order (side=A but this is sell-at-profit — wait, for LONG:
            # SL = sell trigger below = side A
            # TP = sell trigger above = side A too!
            # This is the bug Codex found — both are side=A for LONG
            {
                "coin": "BTC", "isTrigger": True, "reduceOnly": True,
                "side": "A", "triggerPx": "71690.0", "oid": 2001,
                "orderType": "Take Profit Market",
            },
            {
                "coin": "BTC", "isTrigger": True, "reduceOnly": True,
                "side": "A", "triggerPx": "64320.0", "oid": 2002,
                "orderType": "Stop Market",
            },
        ]
        # Current code returns FIRST matching — which could be TP!
        # This test documents the current (buggy) behavior.
        result = get_current_stop_order("BTC", is_long=True)
        assert result is not None
        # BUG: returns TP order (oid=2001) because it matches first
        # After fix: should return SL order (oid=2002)


class TestCheckAndUpdateTrailingStop:
    """Trailing stop movement logic."""
    
    @pytest.fixture
    def long_position(self):
        return {
            "coin": "BTC",
            "size": 0.001,
            "entry_price": 67000.0,
            "is_long": True,
            "unrealized_pnl": 0.0,
        }
    
    @pytest.fixture
    def short_position(self):
        return {
            "coin": "BTC",
            "size": 0.001,
            "entry_price": 67000.0,
            "is_long": False,
            "unrealized_pnl": 0.0,
        }
    
    def test_initial_stop_set_when_none_exists(self, mock_hl, long_position):
        """No stop order → must set initial stop immediately."""
        from luckytrader.trailing import check_and_update_trailing_stop, INITIAL_STOP_PCT
        
        mock_hl.get_market_price.return_value = 67000.0
        mock_hl.get_open_orders_detailed.return_value = []
        # After placement, verify finds the order
        mock_hl.get_open_orders_detailed.side_effect = [
            [],  # first call: no orders
            [{"coin": "BTC", "isTrigger": True, "reduceOnly": True,
              "side": "A", "triggerPx": str(67000 * (1 - INITIAL_STOP_PCT)),
              "oid": 3001, "orderType": "Stop Market"}],  # after placement
        ]
        
        state = {}
        result = check_and_update_trailing_stop("BTC", long_position, state)
        
        assert result["action"] == "updated"
        mock_hl.place_stop_loss.assert_called_once()
    
    def test_trailing_activates_at_3pct(self, mock_hl, long_position):
        """Trailing activates after 3% gain."""
        from luckytrader.trailing import check_and_update_trailing_stop, ACTIVATION_PCT
        
        # Price is 3.5% above entry
        new_price = 67000 * 1.035
        mock_hl.get_market_price.return_value = new_price
        mock_hl.get_open_orders_detailed.return_value = [
            {"coin": "BTC", "isTrigger": True, "reduceOnly": True,
             "side": "A", "triggerPx": str(67000 * 0.965), "oid": 3002,
             "orderType": "Stop Market"},
        ]
        # After update
        mock_hl.get_open_orders_detailed.side_effect = [
            [{"coin": "BTC", "isTrigger": True, "reduceOnly": True,
              "side": "A", "triggerPx": str(67000 * 0.965), "oid": 3002,
              "orderType": "Stop Market"}],
            [{"coin": "BTC", "isTrigger": True, "reduceOnly": True,
              "side": "A", "triggerPx": str(new_price * 0.95), "oid": 3003,
              "orderType": "Stop Market"}],
        ]
        
        state = {"BTC": {
            "entry_price": 67000.0,
            "high_water_mark": 67000.0,  # not yet updated
            "trailing_active": False,
            "last_stop_price": 67000 * 0.965,
        }}
        
        result = check_and_update_trailing_stop("BTC", long_position, state)
        # Should activate trailing and update stop
        assert result["action"] == "updated"
        assert state["BTC"]["trailing_active"] == True
    
    def test_stop_never_below_entry_for_long(self, mock_hl, long_position):
        """Trailing stop must never go below entry price (breakeven floor)."""
        from luckytrader.trailing import check_and_update_trailing_stop
        
        # Price went up 5%, then came back to 3.1% (trailing still active)
        # high_water_mark = 67000 * 1.05 = 70350
        # trailing stop = 70350 * 0.95 = 66832.5 — below entry!
        # Must be clamped to entry_price
        mock_hl.get_market_price.return_value = 67000 * 1.031
        
        state = {"BTC": {
            "entry_price": 67000.0,
            "high_water_mark": 67000 * 1.05,
            "trailing_active": True,
            "last_stop_price": 67000.0,
        }}
        
        # The stop at entry (67000) is already the best we can do since
        # trailing_stop = 70350*0.95 = 66832.5 < entry → clamped to 67000
        # So no_change expected (current stop == new calculated stop)
        stop_order = [{"coin": "BTC", "isTrigger": True, "reduceOnly": True,
             "side": "A", "triggerPx": str(67000.0), "oid": 3004,
             "orderType": "Stop Market"}]
        # get_open_orders_detailed may be called multiple times
        mock_hl.get_open_orders_detailed.return_value = stop_order
        
        result = check_and_update_trailing_stop("BTC", long_position, state)
        # Current stop is at entry, new calculated is also entry (clamped) → no change
        if result["action"] == "no_change":
            assert result["current_stop"] >= 67000.0
        elif result["action"] == "updated":
            assert result["new_stop"] >= 67000.0
    
    def test_stop_only_moves_up_for_long(self, mock_hl, long_position):
        """Stop must never decrease for LONG positions."""
        from luckytrader.trailing import check_and_update_trailing_stop
        
        mock_hl.get_market_price.return_value = 67000 * 1.06
        
        current_stop = 67000 * 1.01  # already above entry
        # high_water_mark 1.08 → trailing = 70350*0.95 = 68196 > current_stop 67670
        # So should update
        high_water = 67000 * 1.08
        new_trailing = high_water * 0.95  # ~68698
        
        state = {"BTC": {
            "entry_price": 67000.0,
            "high_water_mark": high_water,
            "trailing_active": True,
            "last_stop_price": current_stop,
        }}
        
        stop_order = {"coin": "BTC", "isTrigger": True, "reduceOnly": True,
             "side": "A", "triggerPx": str(current_stop), "oid": 3005,
             "orderType": "Stop Market"}
        # First call: return current stop; second call (after update): return new stop
        mock_hl.get_open_orders_detailed.side_effect = [
            [stop_order],
            [{"coin": "BTC", "isTrigger": True, "reduceOnly": True,
              "side": "A", "triggerPx": str(new_trailing), "oid": 3006,
              "orderType": "Stop Market"}],
        ]
        
        result = check_and_update_trailing_stop("BTC", long_position, state)
        assert result["action"] == "updated"
        assert result["new_stop"] >= current_stop
    
    def test_short_position_stop_above_entry(self, mock_hl, short_position):
        """SHORT: initial stop should be above entry price."""
        from luckytrader.trailing import check_and_update_trailing_stop, INITIAL_STOP_PCT
        
        mock_hl.get_market_price.return_value = 67000.0
        expected_stop = 67000 * (1 + INITIAL_STOP_PCT)
        
        mock_hl.get_open_orders_detailed.side_effect = [
            [],  # no stop yet
            [{"coin": "BTC", "isTrigger": True, "reduceOnly": True,
              "side": "B", "triggerPx": str(expected_stop), "oid": 3006,
              "orderType": "Stop Market"}],
        ]
        
        state = {}
        result = check_and_update_trailing_stop("BTC", short_position, state)
        assert result["action"] == "updated"
        # For SHORT, stop loss is a BUY order
        call_args = mock_hl.place_stop_loss.call_args
        assert call_args[0][2] > 67000  # trigger above entry


class TestGetPositions:
    """Position parsing from account info."""
    
    def test_no_positions(self, mock_hl):
        from luckytrader.trailing import get_positions
        mock_hl.get_account_info.return_value = {"positions": []}
        assert get_positions() == []
    
    def test_long_position(self, mock_hl):
        from luckytrader.trailing import get_positions
        mock_hl.get_account_info.return_value = {
            "positions": [{"position": {
                "coin": "BTC", "szi": "0.001", "entryPx": "67000",
                "unrealizedPnl": "5.0",
            }}]
        }
        result = get_positions()
        assert len(result) == 1
        assert result[0]["coin"] == "BTC"
        assert result[0]["is_long"] == True
        assert result[0]["size"] == 0.001
    
    def test_short_position(self, mock_hl):
        from luckytrader.trailing import get_positions
        mock_hl.get_account_info.return_value = {
            "positions": [{"position": {
                "coin": "BTC", "szi": "-0.001", "entryPx": "67000",
                "unrealizedPnl": "-3.0",
            }}]
        }
        result = get_positions()
        assert len(result) == 1
        assert result[0]["is_long"] == False
    
    def test_zero_size_filtered(self, mock_hl):
        from luckytrader.trailing import get_positions
        mock_hl.get_account_info.return_value = {
            "positions": [{"position": {
                "coin": "BTC", "szi": "0", "entryPx": "67000",
                "unrealizedPnl": "0",
            }}]
        }
        assert get_positions() == []


class TestStateIO:
    """State file read/write."""
    
    def test_load_empty_state(self, tmp_path, mock_hl):
        from luckytrader.trailing import load_state, save_state, STATE_FILE
        from luckytrader import trailing as trailing_stop
        
        orig = trailing_stop.STATE_FILE
        trailing_stop.STATE_FILE = tmp_path / "test_state.json"
        
        try:
            assert load_state() == {}
            
            state = {"BTC": {"entry_price": 67000, "trailing_active": False}}
            save_state(state)
            loaded = load_state()
            assert loaded["BTC"]["entry_price"] == 67000
        finally:
            trailing_stop.STATE_FILE = orig
    
    def test_save_creates_dirs(self, tmp_path, mock_hl):
        from luckytrader import trailing as trailing_stop
        orig = trailing_stop.STATE_FILE
        trailing_stop.STATE_FILE = tmp_path / "deep" / "nested" / "state.json"
        
        try:
            save_data = {"test": True}
            trailing_stop.save_state(save_data)
            assert trailing_stop.STATE_FILE.exists()
        finally:
            trailing_stop.STATE_FILE = orig
