"""Additional trailing stop tests for branch coverage."""
import json
import pytest
from unittest.mock import patch, MagicMock


class TestGetRegimeSlPct:
    """Cover _get_regime_sl_pct branches."""

    def test_reads_regime_sl_pct_from_state(self, mock_hl):
        from luckytrader.trailing import _get_regime_sl_pct
        with patch('luckytrader.trailing.load_state') as mock_load:
            # Mock execute.load_state to return regime_sl_pct
            with patch('luckytrader.execute.load_state', return_value={
                "position": {"regime_sl_pct": 0.06, "regime": "volatile"}
            }):
                result = _get_regime_sl_pct("BTC")
                assert result == 0.06

    def test_exception_falls_back_to_config(self, mock_hl):
        from luckytrader.trailing import _get_regime_sl_pct, _cfg
        with patch('luckytrader.execute.load_state', side_effect=Exception("file not found")):
            result = _get_regime_sl_pct("BTC")
            assert result == _cfg.risk.stop_loss_pct


class TestPositionChangeDetection:
    """Cover position change reset branch in check_and_update_trailing_stop."""

    def test_position_change_resets_state(self, mock_hl):
        from luckytrader.trailing import check_and_update_trailing_stop
        mock_hl.get_market_price.return_value = 70000.0
        mock_hl.get_open_orders_detailed.return_value = [{
            "coin": "BTC", "isTrigger": True, "reduceOnly": True,
            "side": "A", "triggerPx": "66000", "oid": 999,
            "orderType": "Stop Market",
        }]
        # Simulate existing trailing state for old entry
        state = {"BTC": {
            "entry_price": 65000.0,  # old entry
            "high_water_mark": 68000.0,
            "trailing_active": True,
            "last_stop_price": 66000.0,
        }}
        position = {
            "coin": "BTC", "size": 0.001, "entry_price": 70000.0,  # new entry, different
            "is_long": True, "unrealized_pnl": 0,
        }
        with patch('luckytrader.execute.load_state', return_value={"position": {}}):
            result = check_and_update_trailing_stop("BTC", position, state)
        # State should be reset
        assert state["BTC"]["entry_price"] == 70000.0
        assert state["BTC"]["trailing_active"] is False


class TestMainBranches:
    """Cover main() function branches."""

    def test_api_exception_returns_early(self, mock_hl):
        from luckytrader.trailing import main
        with patch('luckytrader.trailing.get_positions', side_effect=Exception("api down")):
            result = main()
            assert result is None

    def test_no_positions_with_local_state_skips_cleanup(self, mock_hl):
        """API returns no positions but position_state.json has a record → skip cleanup."""
        from luckytrader.trailing import main
        with patch('luckytrader.trailing.get_positions', return_value=[]), \
             patch('luckytrader.execute.load_state', return_value={"position": {"coin": "BTC"}}):
            result = main()
            assert result is None

    def test_no_positions_cleans_stale_state(self, mock_hl, tmp_path):
        """API returns no positions and no local position → clean stale trailing state."""
        from luckytrader.trailing import main, STATE_FILE
        import luckytrader.trailing as _mod
        # Write stale trailing state
        state_file = tmp_path / "trailing_state.json"
        state_file.write_text(json.dumps({
            "BTC": {"entry_price": 65000, "direction": "LONG",
                    "highest_price": 67000, "stop_loss": 63000}
        }))
        with patch('luckytrader.trailing.get_positions', return_value=[]), \
             patch('luckytrader.execute.load_state', return_value={"position": None}), \
             patch('luckytrader.trailing.STATE_FILE', state_file), \
             patch('luckytrader.execute.notify_discord'):
            result = main()
        # Should have cleaned state
        cleaned = json.loads(state_file.read_text())
        assert cleaned == {}

    def test_error_action_generates_alert(self, mock_hl):
        """Position where stop verification fails → error action → alert."""
        from luckytrader.trailing import main
        position = {
            "coin": "BTC", "size": 0.001, "entry_price": 67000.0,
            "is_long": True, "unrealized_pnl": 0,
        }
        mock_hl.get_market_price.return_value = 67000.0
        mock_hl.get_open_orders_detailed.return_value = []  # no stop order

        with patch('luckytrader.trailing.get_positions', return_value=[position]), \
             patch('luckytrader.trailing.load_state', return_value={}), \
             patch('luckytrader.trailing.save_state'), \
             patch('luckytrader.execute.load_state', return_value={"position": {}}), \
             patch('luckytrader.trailing.get_current_stop_order', return_value=None), \
             patch('luckytrader.trailing.check_and_update_trailing_stop', return_value={
                 "action": "error", "coin": "BTC", "error": "Stop not verified"
             }):
            alerts = main()
        assert alerts is not None
        assert any("Stop order failed" in a for a in alerts)

    def test_no_change_action_displays(self, mock_hl, capsys):
        """Position with no_change action → displays stop unchanged."""
        from luckytrader.trailing import main
        position = {
            "coin": "BTC", "size": 0.001, "entry_price": 67000.0,
            "is_long": True, "unrealized_pnl": 100,
        }
        mock_hl.get_market_price.return_value = 67500.0
        stop_order = {"oid": 1, "trigger_price": 64500.0, "order_type": "Stop Market", "is_trigger": True}

        with patch('luckytrader.trailing.get_positions', return_value=[position]), \
             patch('luckytrader.trailing.load_state', return_value={}), \
             patch('luckytrader.trailing.save_state'), \
             patch('luckytrader.execute.load_state', return_value={"position": {}}), \
             patch('luckytrader.trailing.get_current_stop_order', return_value=stop_order), \
             patch('luckytrader.trailing.check_and_update_trailing_stop', return_value={
                 "action": "no_change", "coin": "BTC", "current_stop": 64500.0,
                 "calculated_stop": 64500.0, "high_water_mark": 67500.0,
                 "trailing_active": False, "gain_pct": 0.7,
             }):
            alerts = main()
        captured = capsys.readouterr()
        assert "unchanged" in captured.out
