"""
Tests for dry run mode — full pipeline without touching real money.
"""
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone


class TestDryRunExecute:
    """execute(dry_run=True) must NEVER place real orders."""
    
    @patch('luckytrader.execute.analyze')
    @patch('luckytrader.execute.get_position', return_value=None)
    def test_dry_run_hold_signal(self, mock_pos, mock_analyze, mock_hl):
        """Dry run with HOLD signal → shows analysis, no orders."""
        from luckytrader.execute import execute
        
        mock_analyze.return_value = {
            "signal": "HOLD", "price": 67000,
            "signal_reasons": [],
            "breakout": {"up": False, "down": False, "vol_ratio_30m": 0.8, "vol_confirm": False},
        }
        
        with patch('luckytrader.execute.load_state', return_value={"position": None}):
            result = execute(dry_run=True)
        
        assert result["action"] == "HOLD"
        assert result.get("dry_run") == True
        mock_hl.place_market_order.assert_not_called()
    
    @patch('luckytrader.execute.analyze')
    @patch('luckytrader.execute.get_position', return_value=None)
    @patch('luckytrader.execute.get_coin_info', return_value={"szDecimals": 5})
    def test_dry_run_long_signal_no_real_order(self, mock_coin, mock_pos, mock_analyze, mock_hl):
        """Dry run with LONG signal → shows what WOULD happen, NO real order."""
        from luckytrader.execute import execute
        
        mock_hl.get_account_info.return_value = {"account_value": "217.76"}
        mock_analyze.return_value = {
            "signal": "LONG", "price": 67000,
            "signal_reasons": ["突破24h高点$68,000", "30m放量1.5x"],
        }
        
        with patch('luckytrader.execute.load_state', return_value={"position": None}):
            result = execute(dry_run=True)
        
        assert result["action"] == "DRY_RUN_WOULD_OPEN"
        assert result["direction"] == "LONG"
        assert result["size"] > 0
        assert result["sl"] < result["entry"]
        assert result["tp"] > result["entry"]
        # CRITICAL: no real orders placed
        mock_hl.place_market_order.assert_not_called()
        mock_hl.place_stop_loss.assert_not_called()
        mock_hl.place_take_profit.assert_not_called()
    
    @patch('luckytrader.execute.analyze')
    @patch('luckytrader.execute.get_position', return_value=None)
    @patch('luckytrader.execute.get_coin_info', return_value={"szDecimals": 5})
    def test_dry_run_short_signal(self, mock_coin, mock_pos, mock_analyze, mock_hl):
        """Dry run SHORT signal."""
        from luckytrader.execute import execute
        
        mock_hl.get_account_info.return_value = {"account_value": "217.76"}
        mock_analyze.return_value = {
            "signal": "SHORT", "price": 67000,
            "signal_reasons": ["跌破24h低点$66,000", "30m放量2.0x"],
        }
        
        with patch('luckytrader.execute.load_state', return_value={"position": None}):
            result = execute(dry_run=True)
        
        assert result["action"] == "DRY_RUN_WOULD_OPEN"
        assert result["direction"] == "SHORT"
        assert result["sl"] > result["entry"]
        assert result["tp"] < result["entry"]
        mock_hl.place_market_order.assert_not_called()
    
    def test_dry_run_with_existing_position(self, mock_hl):
        """Dry run with existing position → shows status, no modifications."""
        from luckytrader.execute import execute
        from datetime import timedelta
        
        position = {
            "coin": "BTC", "size": 0.001, "direction": "LONG",
            "entry_price": 67000.0, "unrealized_pnl": 50.0,
            "liquidation_price": 0,
        }
        state = {
            "position": {
                "coin": "BTC", "direction": "LONG", "size": 0.001,
                "entry_price": 67000.0,
                "entry_time": datetime.now(timezone.utc).isoformat(),
                "sl_price": 64320.0, "tp_price": 71690.0,
                "max_hold_hours": 72,
            }
        }
        
        with patch('luckytrader.execute.get_position', return_value=position):
            with patch('luckytrader.execute.load_state', return_value=state):
                with patch('luckytrader.execute.check_sl_tp_orders', return_value=(True, True)):
                    result = execute(dry_run=True)
        
        assert result["action"] == "HOLD"
        assert result.get("dry_run") == True
    
    @patch('luckytrader.execute.notify_discord')
    def test_dry_run_timeout_no_close(self, mock_notify, mock_hl):
        """Dry run with timed-out position → reports but does NOT close."""
        from luckytrader.execute import execute, MAX_HOLD_HOURS
        from datetime import timedelta
        
        entry_time = (datetime.now(timezone.utc) - timedelta(hours=MAX_HOLD_HOURS + 1)).isoformat()
        state = {
            "position": {
                "coin": "BTC", "direction": "LONG", "size": 0.001,
                "entry_price": 67000.0, "entry_time": entry_time,
                "sl_price": 64320.0, "tp_price": 71690.0,
                "max_hold_hours": MAX_HOLD_HOURS,
            }
        }
        position = {
            "coin": "BTC", "size": 0.001, "direction": "LONG",
            "entry_price": 67000.0, "unrealized_pnl": 50.0,
            "liquidation_price": 0,
        }
        
        with patch('luckytrader.execute.get_position', return_value=position):
            with patch('luckytrader.execute.load_state', return_value=state):
                result = execute(dry_run=True)
        
        assert result["action"] == "DRY_RUN_WOULD_TIMEOUT_CLOSE"
        mock_hl.place_market_order.assert_not_called()
        mock_notify.assert_not_called()
