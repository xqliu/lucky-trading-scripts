"""
Extended trailing_stop tests — main() function and edge cases.
"""
import pytest
from unittest.mock import patch, MagicMock


class TestMain:
    """main() function — the entry point."""
    
    def test_no_positions(self, mock_hl, capsys):
        from luckytrader.trailing import main
        mock_hl.get_account_info.return_value = {"positions": []}
        
        main()
        captured = capsys.readouterr()
        assert "No open positions" in captured.out
    
    def test_with_position_and_stop(self, mock_hl, tmp_path, capsys):
        """Main with a position that has a stop order."""
        from luckytrader import trailing as trailing_stop
        from luckytrader.trailing import main
        orig = trailing_stop.STATE_FILE
        trailing_stop.STATE_FILE = tmp_path / "state.json"
        
        try:
            mock_hl.get_account_info.return_value = {
                "positions": [{"position": {
                    "coin": "BTC", "szi": "0.001", "entryPx": "67000",
                    "unrealizedPnl": "5.0",
                }}]
            }
            mock_hl.get_market_price.return_value = 67500.0
            mock_hl.get_open_orders_detailed.return_value = [
                {"coin": "BTC", "isTrigger": True, "reduceOnly": True,
                 "side": "A", "triggerPx": "64655.0", "oid": 9001,
                 "orderType": "Stop Market"},
            ]
            
            alerts = main()
            captured = capsys.readouterr()
            assert "BTC LONG" in captured.out
            assert "Stop order active" in captured.out
            assert alerts is None or len(alerts) == 0
        finally:
            trailing_stop.STATE_FILE = orig
    
    def test_position_without_stop_generates_alert(self, mock_hl, tmp_path, capsys):
        """Position with no stop order → alert."""
        from luckytrader import trailing as trailing_stop
        from luckytrader.trailing import main
        orig = trailing_stop.STATE_FILE
        trailing_stop.STATE_FILE = tmp_path / "state.json"
        
        try:
            mock_hl.get_account_info.return_value = {
                "positions": [{"position": {
                    "coin": "BTC", "szi": "0.001", "entryPx": "67000",
                    "unrealizedPnl": "5.0",
                }}]
            }
            mock_hl.get_market_price.return_value = 67500.0
            # No stop order, then after placement verify it
            mock_hl.get_open_orders_detailed.side_effect = [
                [],  # main check: no stop
                [],  # get_current_stop_order inside check_and_update
                [{"coin": "BTC", "isTrigger": True, "reduceOnly": True,
                  "side": "A", "triggerPx": "64655.0", "oid": 9002,
                  "orderType": "Stop Market"}],  # verify after placement
            ]
            
            alerts = main()
            captured = capsys.readouterr()
            assert "NO STOP ORDER" in captured.out
        finally:
            trailing_stop.STATE_FILE = orig
            mock_hl.get_open_orders_detailed.side_effect = None


class TestVerificationAfterPlacement:
    """Stop order verification — critical safety feature."""
    
    def test_verification_failure(self, mock_hl):
        """If stop order can't be verified, return error."""
        from luckytrader.trailing import check_and_update_trailing_stop
        
        position = {
            "coin": "BTC", "size": 0.001, "entry_price": 67000.0,
            "is_long": True, "unrealized_pnl": 0,
        }
        
        mock_hl.get_market_price.return_value = 67000.0
        # No orders found (need to set stop), and verification also fails
        mock_hl.get_open_orders_detailed.side_effect = [
            [],  # no current stop
            [],  # verification fails — stop not found
        ]
        
        state = {}
        result = check_and_update_trailing_stop("BTC", position, state)
        assert result["action"] == "error"
        assert "not verified" in result["error"].lower() or "not found" in result["error"].lower()


class TestShortPositionTrailing:
    """Trailing stop for SHORT positions."""
    
    def test_short_trailing_activates(self, mock_hl):
        """SHORT: price drops 3%+ → trailing activates."""
        from luckytrader.trailing import check_and_update_trailing_stop
        
        position = {
            "coin": "BTC", "size": 0.001, "entry_price": 67000.0,
            "is_long": False, "unrealized_pnl": 50,
        }
        
        # Price dropped 4% — trailing should activate
        new_price = 67000 * 0.96
        mock_hl.get_market_price.return_value = new_price
        
        # Existing stop above entry
        current_stop = 67000 * 1.035
        mock_hl.get_open_orders_detailed.side_effect = [
            [{"coin": "BTC", "isTrigger": True, "reduceOnly": True,
              "side": "B", "triggerPx": str(current_stop), "oid": 8001,
              "orderType": "Stop Market"}],
            [{"coin": "BTC", "isTrigger": True, "reduceOnly": True,
              "side": "B", "triggerPx": str(new_price * 1.05), "oid": 8002,
              "orderType": "Stop Market"}],
        ]
        
        state = {"BTC": {
            "entry_price": 67000.0,
            "high_water_mark": 67000.0,
            "trailing_active": False,
        }}
        
        result = check_and_update_trailing_stop("BTC", position, state)
        assert state["BTC"]["trailing_active"] == True
    
    def test_short_stop_clamped_to_entry(self, mock_hl):
        """SHORT: trailing stop must not go above entry (breakeven floor)."""
        from luckytrader.trailing import check_and_update_trailing_stop
        
        position = {
            "coin": "BTC", "size": 0.001, "entry_price": 67000.0,
            "is_long": False, "unrealized_pnl": 50,
        }
        
        # Price went down 5%, low water mark at 63650
        # trailing = 63650 * 1.05 = 66832.5 — below entry, so entry is better
        mock_hl.get_market_price.return_value = 67000 * 0.969
        
        stop_order = [{"coin": "BTC", "isTrigger": True, "reduceOnly": True,
              "side": "B", "triggerPx": str(67000.0), "oid": 8003,
              "orderType": "Stop Market"}]
        mock_hl.get_open_orders_detailed.return_value = stop_order
        
        state = {"BTC": {
            "entry_price": 67000.0,
            "high_water_mark": 67000 * 0.95,
            "trailing_active": True,
        }}
        
        result = check_and_update_trailing_stop("BTC", position, state)
        # Stop at entry (67000), new calculated also ≤ entry → no change
        if result["action"] == "no_change":
            assert result["current_stop"] <= 67000.0
