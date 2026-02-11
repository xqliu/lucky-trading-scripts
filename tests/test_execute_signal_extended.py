"""
Extended tests for execute_signal.py — covering remaining branches.
"""
import json
import pytest
from unittest.mock import patch, MagicMock, call
from datetime import datetime, timezone, timedelta
from pathlib import Path


class TestOpenPositionFlow:
    """Detailed open_position branch coverage."""
    
    @patch('luckytrader.execute.notify_discord')
    @patch('luckytrader.execute.get_coin_info', return_value={"szDecimals": 5})
    @patch('luckytrader.execute.log_trade')
    def test_successful_long_open(self, mock_log, mock_coin, mock_notify, mock_hl):
        """Full successful LONG open flow."""
        from luckytrader.execute import open_position
        
        mock_hl.get_account_info.return_value = {"account_value": "217.76"}
        mock_hl.place_market_order.return_value = {"status": "ok"}
        mock_hl.place_stop_loss.return_value = {"status": "ok"}
        mock_hl.place_take_profit.return_value = {"status": "ok"}
        
        with patch('luckytrader.execute.get_position') as mock_pos:
            mock_pos.return_value = {
                "coin": "BTC", "size": 0.001, "direction": "LONG",
                "entry_price": 67000.0, "unrealized_pnl": 0,
                "liquidation_price": 0,
            }
            with patch('luckytrader.execute.save_state') as mock_save:
                result = open_position("LONG", {"price": 67000, "signal_reasons": ["test"]})
        
        assert result["action"] == "OPENED"
        assert result["direction"] == "LONG"
        assert result["sl"] < result["entry"]
        assert result["tp"] > result["entry"]
        mock_hl.place_stop_loss.assert_called_once()
        mock_hl.place_take_profit.assert_called_once()
        # Verify notification uses correct params
        notify_call = mock_notify.call_args[0][0]
        assert '-4%' in notify_call
        assert '+7%' in notify_call
        assert '72h' in notify_call
    
    @patch('luckytrader.execute.notify_discord')
    @patch('luckytrader.execute.get_coin_info', return_value={"szDecimals": 5})
    @patch('luckytrader.execute.log_trade')
    def test_successful_short_open(self, mock_log, mock_coin, mock_notify, mock_hl):
        """Full successful SHORT open flow."""
        from luckytrader.execute import open_position
        
        mock_hl.get_account_info.return_value = {"account_value": "217.76"}
        mock_hl.place_market_order.return_value = {"status": "ok"}
        mock_hl.place_stop_loss.return_value = {"status": "ok"}
        mock_hl.place_take_profit.return_value = {"status": "ok"}
        
        with patch('luckytrader.execute.get_position') as mock_pos:
            mock_pos.return_value = {
                "coin": "BTC", "size": -0.001, "direction": "SHORT",
                "entry_price": 67000.0, "unrealized_pnl": 0,
                "liquidation_price": 0,
            }
            with patch('luckytrader.execute.save_state'):
                result = open_position("SHORT", {"price": 67000, "signal_reasons": ["test"]})
        
        assert result["action"] == "OPENED"
        assert result["direction"] == "SHORT"
        assert result["sl"] > result["entry"]
        assert result["tp"] < result["entry"]
    
    @patch('luckytrader.execute.notify_discord')
    @patch('luckytrader.execute.get_coin_info', return_value={"szDecimals": 5})
    def test_open_failed_error_status(self, mock_coin, mock_notify, mock_hl):
        """Market order returns error status."""
        from luckytrader.execute import open_position
        
        mock_hl.get_account_info.return_value = {"account_value": "217.76"}
        mock_hl.place_market_order.return_value = {"status": "err", "response": "insufficient margin"}
        
        result = open_position("LONG", {"price": 67000, "signal_reasons": []})
        assert result["action"] == "OPEN_FAILED"
    
    @patch('luckytrader.execute.notify_discord')
    @patch('luckytrader.execute.get_coin_info', return_value={"szDecimals": 5})
    def test_open_no_position_after_order(self, mock_coin, mock_notify, mock_hl):
        """Order succeeds but position not found."""
        from luckytrader.execute import open_position
        
        mock_hl.get_account_info.return_value = {"account_value": "217.76"}
        mock_hl.place_market_order.return_value = {"status": "ok"}
        
        with patch('luckytrader.execute.get_position', return_value=None):
            result = open_position("LONG", {"price": 67000, "signal_reasons": []})
        
        assert result["action"] == "OPEN_FAILED"
    
    @patch('luckytrader.execute.get_coin_info', return_value={"szDecimals": 5})
    def test_size_too_small(self, mock_coin, mock_hl):
        """Account too small → skip."""
        from luckytrader.execute import open_position
        
        mock_hl.get_account_info.return_value = {"account_value": "0.01"}
        
        result = open_position("LONG", {"price": 67000, "signal_reasons": []})
        assert result["action"] == "SKIP"
    
    @patch('luckytrader.execute.get_coin_info', return_value=None)
    def test_no_coin_info_defaults(self, mock_coin, mock_hl):
        """Missing coin info → use default szDecimals=5."""
        from luckytrader.execute import open_position
        
        mock_hl.get_account_info.return_value = {"account_value": "217.76"}
        mock_hl.place_market_order.return_value = {"status": "ok"}
        mock_hl.place_stop_loss.return_value = {"status": "ok"}
        mock_hl.place_take_profit.return_value = {"status": "ok"}
        
        with patch('luckytrader.execute.get_position') as mock_pos:
            mock_pos.return_value = {
                "coin": "BTC", "size": 0.001, "direction": "LONG",
                "entry_price": 67000.0, "unrealized_pnl": 0,
                "liquidation_price": 0,
            }
            with patch('luckytrader.execute.save_state'):
                with patch('luckytrader.execute.log_trade'):
                    with patch('luckytrader.execute.notify_discord'):
                        result = open_position("LONG", {"price": 67000, "signal_reasons": []})
        
        assert result["action"] == "OPENED"


class TestExistingPositionBranches:
    """Branches when a position already exists."""
    
    @patch('luckytrader.execute.analyze')
    def test_existing_position_with_sl_tp(self, mock_analyze, mock_hl):
        """Existing position with SL/TP intact → HOLD."""
        from luckytrader.execute import execute
        
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
                    result = execute()
        
        assert result["action"] == "HOLD"
        assert result["position"] == position
    
    @patch('luckytrader.execute.analyze')
    @patch('luckytrader.execute.fix_sl_tp')
    def test_missing_sl_triggers_fix(self, mock_fix, mock_analyze, mock_hl):
        """SL missing → fix_sl_tp called."""
        from luckytrader.execute import execute
        
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
                with patch('luckytrader.execute.check_sl_tp_orders', return_value=(False, True)):
                    result = execute()
        
        mock_fix.assert_called_once_with(position)
    
    @patch('luckytrader.execute.notify_discord')
    def test_sl_triggered_short(self, mock_notify, mock_hl):
        """SHORT position closed by SL (price went up)."""
        from luckytrader.execute import execute
        
        state = {
            "position": {
                "coin": "BTC", "direction": "SHORT", "size": 0.001,
                "entry_price": 67000.0,
                "entry_time": datetime.now(timezone.utc).isoformat(),
                "sl_price": 69680.0, "tp_price": 62310.0,
            }
        }
        
        mock_hl.get_market_price.return_value = 69800.0  # above SL
        
        with patch('luckytrader.execute.get_position', return_value=None):
            with patch('luckytrader.execute.load_state', return_value=state):
                with patch('luckytrader.execute.save_state'):
                    with patch('luckytrader.execute.record_trade_result'):
                        with patch('luckytrader.execute.log_trade'):
                            result = execute()
        
        assert result["action"] == "CLOSED_BY_TRIGGER"
        assert result["reason"] == "SL"
    
    def test_position_no_entry_time(self, mock_hl):
        """Position exists but state has no entry_time → no timeout check."""
        from luckytrader.execute import execute
        
        position = {
            "coin": "BTC", "size": 0.001, "direction": "LONG",
            "entry_price": 67000.0, "unrealized_pnl": 50.0,
            "liquidation_price": 0,
        }
        state = {"position": {"coin": "BTC", "direction": "LONG"}}
        
        with patch('luckytrader.execute.get_position', return_value=position):
            with patch('luckytrader.execute.load_state', return_value=state):
                with patch('luckytrader.execute.check_sl_tp_orders', return_value=(True, True)):
                    result = execute()
        
        assert result["action"] == "HOLD"


class TestFixSlTp:
    """SL/TP repair logic."""
    
    def test_fix_missing_sl(self, mock_hl):
        from luckytrader.execute import fix_sl_tp
        
        position = {
            "coin": "BTC", "size": 0.001, "direction": "LONG",
            "entry_price": 67000.0,
        }
        
        mock_hl.get_open_orders_detailed.return_value = []  # no orders
        
        fix_sl_tp(position)
        
        mock_hl.place_stop_loss.assert_called_once()
        mock_hl.place_take_profit.assert_called_once()
    
    def test_fix_sl_failure_triggers_emergency(self, mock_hl):
        from luckytrader.execute import fix_sl_tp
        
        position = {
            "coin": "BTC", "size": 0.001, "direction": "LONG",
            "entry_price": 67000.0,
        }
        
        mock_hl.get_open_orders_detailed.return_value = []
        mock_hl.place_stop_loss.side_effect = Exception("Network error")
        
        with patch('luckytrader.execute.emergency_close') as mock_emg:
            with patch('luckytrader.execute.save_state'):
                with patch('luckytrader.execute.log_trade'):
                    fix_sl_tp(position)
            mock_emg.assert_called_once()
        
        # Reset side_effect
        mock_hl.place_stop_loss.side_effect = None
    
    def test_fix_tp_only_missing(self, mock_hl):
        """Only TP missing, SL exists."""
        from luckytrader.execute import fix_sl_tp
        
        position = {
            "coin": "BTC", "size": 0.001, "direction": "LONG",
            "entry_price": 67000.0,
        }
        
        # SL exists
        mock_hl.get_open_orders_detailed.return_value = [
            {"coin": "BTC", "isTrigger": True, "orderType": "Stop Market"},
        ]
        
        fix_sl_tp(position)
        mock_hl.place_stop_loss.assert_not_called()
        mock_hl.place_take_profit.assert_called_once()
    
    def test_fix_short_position(self, mock_hl):
        """Fix SL/TP for SHORT position — directions reversed."""
        from luckytrader.execute import fix_sl_tp, STOP_LOSS_PCT, TAKE_PROFIT_PCT
        
        position = {
            "coin": "BTC", "size": 0.001, "direction": "SHORT",
            "entry_price": 67000.0,
        }
        
        mock_hl.get_open_orders_detailed.return_value = []
        
        fix_sl_tp(position)
        
        # SL for SHORT should be above entry
        sl_call = mock_hl.place_stop_loss.call_args
        sl_price = sl_call[0][2]
        assert sl_price > 67000.0
        
        # TP for SHORT should be below entry
        tp_call = mock_hl.place_take_profit.call_args
        tp_price = tp_call[0][2]
        assert tp_price < 67000.0


class TestClosePosition:
    """Normal close (timeout)."""
    
    def test_close_cancels_orders_first(self, mock_hl):
        from luckytrader.execute import close_position
        
        position = {
            "coin": "BTC", "size": 0.001, "direction": "LONG",
            "entry_price": 67000.0,
        }
        
        mock_hl.get_open_orders_detailed.return_value = [
            {"coin": "BTC", "oid": 5001},
            {"coin": "BTC", "oid": 5002},
        ]
        
        with patch('luckytrader.execute.save_state'):
            with patch('luckytrader.execute.log_trade'):
                close_position(position)
        
        assert mock_hl.cancel_order.call_count == 2
        mock_hl.place_market_order.assert_called_once_with("BTC", False, 0.001)


class TestGetPosition:
    """Position fetching from exchange."""
    
    def test_get_position_found(self, mock_hl):
        from luckytrader.execute import get_position
        
        # Mock the Info class
        with patch('luckytrader.execute.Info') as MockInfo:
            mock_info = MagicMock()
            MockInfo.return_value = mock_info
            mock_info.user_state.return_value = {
                "assetPositions": [
                    {"position": {
                        "coin": "BTC", "szi": "0.001", "entryPx": "67000",
                        "unrealizedPnl": "5.0", "liquidationPx": "60000",
                    }}
                ]
            }
            
            result = get_position("BTC")
        
        assert result is not None
        assert result["coin"] == "BTC"
        assert result["direction"] == "LONG"
    
    def test_get_position_not_found(self, mock_hl):
        from luckytrader.execute import get_position
        
        with patch('luckytrader.execute.Info') as MockInfo:
            mock_info = MagicMock()
            MockInfo.return_value = mock_info
            mock_info.user_state.return_value = {
                "assetPositions": [
                    {"position": {
                        "coin": "ETH", "szi": "0.5", "entryPx": "1975",
                        "unrealizedPnl": "10.0",
                    }}
                ]
            }
            
            result = get_position("BTC")
        
        assert result is None
    
    def test_get_position_zero_size(self, mock_hl):
        from luckytrader.execute import get_position
        
        with patch('luckytrader.execute.Info') as MockInfo:
            mock_info = MagicMock()
            MockInfo.return_value = mock_info
            mock_info.user_state.return_value = {
                "assetPositions": [
                    {"position": {
                        "coin": "BTC", "szi": "0", "entryPx": "67000",
                        "unrealizedPnl": "0",
                    }}
                ]
            }
            
            result = get_position("BTC")
        
        assert result is None


class TestCheckSlTpOrders:
    """SL/TP order detection."""
    
    def test_both_exist(self, mock_hl):
        from luckytrader.execute import check_sl_tp_orders
        
        mock_hl.get_open_orders_detailed.return_value = [
            {"coin": "BTC", "isTrigger": True, "orderType": "Stop Market"},
            {"coin": "BTC", "isTrigger": True, "orderType": "Take Profit Market"},
        ]
        
        sl, tp = check_sl_tp_orders("BTC", {"direction": "LONG"})
        assert sl == True
        assert tp == True
    
    def test_neither_exist(self, mock_hl):
        from luckytrader.execute import check_sl_tp_orders
        
        mock_hl.get_open_orders_detailed.return_value = []
        
        sl, tp = check_sl_tp_orders("BTC", {"direction": "LONG"})
        assert sl == False
        assert tp == False
    
    def test_only_sl(self, mock_hl):
        from luckytrader.execute import check_sl_tp_orders
        
        mock_hl.get_open_orders_detailed.return_value = [
            {"coin": "BTC", "isTrigger": True, "orderType": "Stop Market"},
        ]
        
        sl, tp = check_sl_tp_orders("BTC", {"direction": "LONG"})
        assert sl == True
        assert tp == False


class TestLogTrade:
    """Trade logging to TRADES.md."""
    
    def test_log_trade_creates_file(self, tmp_path, mock_hl):
        from luckytrader import execute as execute_signal
        orig = execute_signal.TRADES_FILE
        execute_signal.TRADES_FILE = tmp_path / "trades" / "TRADES.md"
        
        try:
            execute_signal.log_trade("OPEN", "BTC", "LONG", 0.001, 67000.0,
                                     sl=64320.0, tp=71690.0, reason="test signal")
            
            content = execute_signal.TRADES_FILE.read_text()
            assert "OPEN" in content
            assert "LONG" in content
            assert "BTC" in content
            assert "$67,000" in content
        finally:
            execute_signal.TRADES_FILE = orig


class TestNotifyDiscord:
    """Discord notification."""
    
    def test_notify_catches_errors(self, mock_hl):
        """notify_discord should not raise even if subprocess fails."""
        from luckytrader.execute import notify_discord
        
        with patch('subprocess.run', side_effect=Exception("no such file")):
            # Should not raise
            notify_discord("test message")


class TestGetCoinInfo:
    """Exchange metadata lookup."""
    
    def test_coin_found(self, mock_hl):
        from luckytrader.execute import get_coin_info
        
        with patch('luckytrader.execute.Info') as MockInfo:
            mock_info = MagicMock()
            MockInfo.return_value = mock_info
            mock_info.meta.return_value = {
                "universe": [
                    {"name": "BTC", "szDecimals": 5},
                    {"name": "ETH", "szDecimals": 4},
                ]
            }
            
            result = get_coin_info("BTC")
        
        assert result["szDecimals"] == 5
    
    def test_coin_not_found(self, mock_hl):
        from luckytrader.execute import get_coin_info
        
        with patch('luckytrader.execute.Info') as MockInfo:
            mock_info = MagicMock()
            MockInfo.return_value = mock_info
            mock_info.meta.return_value = {"universe": []}
            
            result = get_coin_info("DOGE")
        
        assert result is None
