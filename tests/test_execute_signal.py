"""
Tests for execute_signal.py — order execution and position management.
This touches REAL MONEY if bugs slip through.
"""
import json
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta
from pathlib import Path


class TestParameterConsistency:
    """P0: Parameters across files MUST match."""
    
    def test_sl_tp_hold_match_signal_check(self):
        """execute_signal params must match signal_check suggestions."""
        from luckytrader.execute import STOP_LOSS_PCT, TAKE_PROFIT_PCT, MAX_HOLD_HOURS
        assert STOP_LOSS_PCT == 0.04, f"SL should be 4%, got {STOP_LOSS_PCT*100}%"
        assert TAKE_PROFIT_PCT == 0.07, f"TP should be 7%, got {TAKE_PROFIT_PCT*100}%"
        assert MAX_HOLD_HOURS == 72, f"Hold should be 72h, got {MAX_HOLD_HOURS}h"
    
    def test_notification_text_matches_params(self):
        """Discord notification must show correct TP% and hold hours."""
        import inspect
        from luckytrader.execute import open_position
        source = inspect.getsource(open_position)
        # The notify_discord call should NOT contain old params
        assert '+5%' not in source or '+7%' in source, \
            "Notification text still says +5% but TP is 7%"
        assert '48h' not in source or '72h' in source, \
            "Notification text still says 48h but hold is 72h"


class TestPositionSizing:
    """Position size calculation — wrong size = wrong risk."""
    
    @patch('luckytrader.execute.analyze')
    @patch('luckytrader.execute.get_position', return_value=None)
    @patch('luckytrader.execute.get_coin_info')
    @patch('luckytrader.execute.notify_discord')
    def test_max_loss_cap(self, mock_notify, mock_coin_info, mock_pos, mock_analyze, mock_hl):
        """Position capped so max loss at SL ≤ $10."""
        from luckytrader.execute import open_position, STOP_LOSS_PCT, MAX_SINGLE_LOSS
        
        mock_coin_info.return_value = {"szDecimals": 5}
        mock_hl.get_account_info.return_value = {"account_value": "1000.0"}
        
        # With $1000 account, 30% = $300 position, 4% SL = $12 loss > $10 cap
        # Should reduce to $10 / 0.04 = $250
        analysis = {
            "price": 67000.0,
            "signal": "LONG",
            "signal_reasons": ["test"],
        }
        
        mock_hl.place_market_order.return_value = {"status": "ok"}
        
        # Mock get_position to return position after order
        with patch('luckytrader.execute.get_position') as mock_get_pos:
            mock_get_pos.side_effect = [
                None,  # first check
                {"coin": "BTC", "size": 0.00373, "direction": "LONG",
                 "entry_price": 67000.0, "unrealized_pnl": 0},
            ]
            with patch('luckytrader.execute.check_sl_tp_orders', return_value=(True, True)):
                with patch('luckytrader.execute.log_trade'):
                    with patch('luckytrader.execute.save_state'):
                        result = open_position("LONG", analysis)
        
        # The market order size should be ≤ $250 / 67000
        if mock_hl.place_market_order.called:
            call_args = mock_hl.place_market_order.call_args
            size = call_args[0][2]  # third arg = size
            max_size = MAX_SINGLE_LOSS / STOP_LOSS_PCT / 67000
            assert size <= max_size + 0.00001, \
                f"Size {size} exceeds max loss cap (max {max_size})"
    
    def test_position_ratio(self, mock_hl):
        """POSITION_RATIO should be 30%."""
        from luckytrader.execute import POSITION_RATIO
        assert POSITION_RATIO == 0.30


class TestExecuteFlow:
    """Main execute() flow — the heartbeat of the system."""
    
    @patch('luckytrader.execute.analyze')
    @patch('luckytrader.execute.get_position', return_value=None)
    def test_no_position_hold_signal(self, mock_pos, mock_analyze, mock_hl):
        """No position + HOLD signal → do nothing."""
        from luckytrader.execute import execute
        
        mock_analyze.return_value = {"signal": "HOLD", "price": 67000}
        
        with patch('luckytrader.execute.load_state', return_value={"position": None}):
            result = execute()
        
        assert result["action"] == "HOLD"
        mock_hl.place_market_order.assert_not_called()
    
    @patch('luckytrader.execute.analyze')
    @patch('luckytrader.execute.get_position', return_value=None)
    def test_signal_error(self, mock_pos, mock_analyze, mock_hl):
        """Signal check error → no trade."""
        from luckytrader.execute import execute
        
        mock_analyze.return_value = {"error": "数据不足"}
        
        with patch('luckytrader.execute.load_state', return_value={"position": None}):
            result = execute()
        
        assert result["action"] == "ERROR"
    
    @patch('luckytrader.execute.notify_discord')
    def test_timeout_close(self, mock_notify, mock_hl):
        """Position held > MAX_HOLD_HOURS → force close."""
        from luckytrader.execute import execute, MAX_HOLD_HOURS
        
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
                with patch('luckytrader.execute.close_position') as mock_close:
                    with patch('luckytrader.execute.record_trade_result'):
                        result = execute()
        
        assert result["action"] == "TIMEOUT_CLOSE"
        mock_close.assert_called_once()
    
    @patch('luckytrader.execute.notify_discord')
    def test_detect_sl_tp_trigger(self, mock_notify, mock_hl):
        """State says position exists but chain says no → SL/TP was triggered."""
        from luckytrader.execute import execute

        state = {
            "position": {
                "coin": "BTC", "direction": "LONG", "size": 0.001,
                "entry_price": 67000.0, "entry_time": datetime.now(timezone.utc).isoformat(),
                "sl_price": 64320.0, "tp_price": 71690.0,
            }
        }

        mock_hl.get_market_price.return_value = 71800.0  # above TP → TP triggered

        with patch('luckytrader.execute.get_position', return_value=None):
            with patch('luckytrader.execute.load_state', return_value=state):
                with patch('luckytrader.execute.save_state') as mock_save:
                    with patch('luckytrader.execute.record_trade_result') as mock_record:
                        with patch('luckytrader.execute.log_trade'):
                            with patch('luckytrader.execute.get_recent_fills', return_value=[]):
                                result = execute()

        assert result["action"] == "CLOSED_BY_TRIGGER"
        assert result["reason"] == "TP"
        mock_save.assert_called_with({"position": None})


class TestEmergencyClose:
    """Emergency close — last line of defense."""
    
    @patch('luckytrader.execute.log_trade')
    def test_emergency_close_calls_market_order(self, mock_log, mock_hl):
        from luckytrader.execute import emergency_close
        
        with patch('luckytrader.execute.save_state'):
            emergency_close("BTC", 0.001, True)
        
        # Should sell to close long
        mock_hl.place_market_order.assert_called_once_with("BTC", False, 0.001)
    
    @patch('luckytrader.execute.log_trade')
    def test_emergency_close_short(self, mock_log, mock_hl):
        from luckytrader.execute import emergency_close
        
        with patch('luckytrader.execute.save_state'):
            emergency_close("BTC", 0.001, False)
        
        # Should buy to close short
        mock_hl.place_market_order.assert_called_once_with("BTC", True, 0.001)


class TestAtomicOpen:
    """Opening position must be atomic: open + SL + TP or rollback."""
    
    @patch('luckytrader.execute.notify_discord')
    @patch('luckytrader.execute.get_coin_info', return_value={"szDecimals": 5})
    @patch('luckytrader.execute.log_trade')
    def test_sl_failure_triggers_emergency_close(self, mock_log, mock_coin, mock_notify, mock_hl):
        """If SL placement fails after opening, must emergency close."""
        from luckytrader.execute import open_position
        
        mock_hl.get_account_info.return_value = {"account_value": "217.76"}
        mock_hl.place_market_order.return_value = {"status": "ok"}
        mock_hl.place_stop_loss.return_value = {"status": "err", "response": "failed"}
        
        with patch('luckytrader.execute.get_position') as mock_pos:
            mock_pos.return_value = {
                "coin": "BTC", "size": 0.001, "direction": "LONG",
                "entry_price": 67000.0, "unrealized_pnl": 0,
                "liquidation_price": 0,
            }
            with patch('luckytrader.execute.emergency_close') as mock_emg:
                with patch('luckytrader.execute.save_state'):
                    result = open_position("LONG", {"price": 67000, "signal_reasons": []})
        
        assert result["action"] == "SL_FAILED_CLOSED"
        mock_emg.assert_called_once()
    
    @patch('luckytrader.execute.notify_discord')
    @patch('luckytrader.execute.get_coin_info', return_value={"szDecimals": 5})
    @patch('luckytrader.execute.log_trade')
    def test_tp_failure_triggers_emergency_close(self, mock_log, mock_coin, mock_notify, mock_hl):
        """If TP placement fails after opening + SL, must emergency close."""
        from luckytrader.execute import open_position
        
        mock_hl.get_account_info.return_value = {"account_value": "217.76"}
        mock_hl.place_market_order.return_value = {"status": "ok"}
        mock_hl.place_stop_loss.return_value = {"status": "ok"}
        mock_hl.place_take_profit.return_value = {"status": "err"}
        
        with patch('luckytrader.execute.get_position') as mock_pos:
            mock_pos.return_value = {
                "coin": "BTC", "size": 0.001, "direction": "LONG",
                "entry_price": 67000.0, "unrealized_pnl": 0,
                "liquidation_price": 0,
            }
            with patch('luckytrader.execute.emergency_close') as mock_emg:
                with patch('luckytrader.execute.save_state'):
                    result = open_position("LONG", {"price": 67000, "signal_reasons": []})
        
        assert result["action"] == "TP_FAILED_CLOSED"


class TestTradeResultTracking:
    """Trade result logging and consecutive loss detection."""
    
    def test_record_trade_result(self, tmp_path, mock_hl):
        from luckytrader import execute as execute_signal
        orig = execute_signal.TRADE_LOG_FILE
        execute_signal.TRADE_LOG_FILE = tmp_path / "results.json"
        
        try:
            with patch('luckytrader.execute.trigger_optimization'):
                execute_signal.record_trade_result(2.5, "LONG", "BTC", "TP")
                execute_signal.record_trade_result(-4.0, "SHORT", "BTC", "SL")
            
            log = json.loads(execute_signal.TRADE_LOG_FILE.read_text())
            assert len(log) == 2
            assert log[0]["pnl_pct"] == 2.5
            assert log[1]["pnl_pct"] == -4.0
        finally:
            execute_signal.TRADE_LOG_FILE = orig
    
    def test_consecutive_losses_trigger_optimization(self, tmp_path, mock_hl):
        from luckytrader import execute as execute_signal
        orig = execute_signal.TRADE_LOG_FILE
        execute_signal.TRADE_LOG_FILE = tmp_path / "results.json"
        
        try:
            with patch('luckytrader.execute.trigger_optimization') as mock_opt:
                # Record 5 consecutive losses
                for i in range(5):
                    execute_signal.record_trade_result(-4.0, "LONG", "BTC", "SL")
                
                mock_opt.assert_called_once()
        finally:
            execute_signal.TRADE_LOG_FILE = orig


class TestStateIO:
    """Position state persistence."""
    
    def test_load_save_state(self, tmp_path, mock_hl):
        from luckytrader import execute as execute_signal
        orig = execute_signal.STATE_FILE
        execute_signal.STATE_FILE = tmp_path / "pos_state.json"
        
        try:
            # Empty state
            assert execute_signal.load_state() == {"position": None}
            
            # Save and reload
            state = {"position": {"coin": "BTC", "direction": "LONG"}}
            execute_signal.save_state(state)
            loaded = execute_signal.load_state()
            assert loaded["position"]["coin"] == "BTC"
        finally:
            execute_signal.STATE_FILE = orig


class TestSlTpTriggerFillPrice:
    """Fix 8: SL/TP trigger detection should use fill price, not market price."""

    @patch('luckytrader.execute.notify_discord')
    def test_sl_tp_trigger_uses_fill_price(self, mock_notify, mock_hl):
        """When SL/TP triggered, PnL should use actual fill price from get_recent_fills."""
        from luckytrader.execute import execute
        from luckytrader.signal import get_recent_fills

        state = {
            "position": {
                "coin": "BTC", "direction": "LONG", "size": 0.001,
                "entry_price": 67000.0, "entry_time": datetime.now(timezone.utc).isoformat(),
                "sl_price": 64320.0, "tp_price": 71690.0,
            }
        }

        # Fill at 71500 (actual TP fill), market drifted to 72000
        fill_data = [{"coin": "BTC", "side": "SELL", "size": "0.001", "price": "71500", "time": 1234567890}]
        mock_hl.get_market_price.return_value = 72000.0

        with patch('luckytrader.execute.get_position', return_value=None), \
             patch('luckytrader.execute.load_state', return_value=state), \
             patch('luckytrader.execute.save_state'), \
             patch('luckytrader.execute.record_trade_result') as mock_record, \
             patch('luckytrader.execute.log_trade'), \
             patch('luckytrader.execute.get_recent_fills', return_value=fill_data):
            result = execute()

        assert result["action"] == "CLOSED_BY_TRIGGER"
        # PnL should be based on fill price 71500, not market 72000
        expected_pnl = (71500 - 67000) / 67000 * 100  # 6.72%
        assert abs(result["pnl_pct"] - expected_pnl) < 0.01, \
            f"PnL {result['pnl_pct']:.2f}% should be {expected_pnl:.2f}% (fill price)"

    @patch('luckytrader.execute.notify_discord')
    def test_sl_tp_trigger_falls_back_to_market_price(self, mock_notify, mock_hl):
        """When no fills available, fall back to market price."""
        from luckytrader.execute import execute

        state = {
            "position": {
                "coin": "BTC", "direction": "LONG", "size": 0.001,
                "entry_price": 67000.0, "entry_time": datetime.now(timezone.utc).isoformat(),
                "sl_price": 64320.0, "tp_price": 71690.0,
            }
        }

        mock_hl.get_market_price.return_value = 72000.0

        with patch('luckytrader.execute.get_position', return_value=None), \
             patch('luckytrader.execute.load_state', return_value=state), \
             patch('luckytrader.execute.save_state'), \
             patch('luckytrader.execute.record_trade_result'), \
             patch('luckytrader.execute.log_trade'), \
             patch('luckytrader.execute.get_recent_fills', return_value=[]):
            result = execute()

        assert result["action"] == "CLOSED_BY_TRIGGER"
        expected_pnl = (72000 - 67000) / 67000 * 100  # 7.46%
        assert abs(result["pnl_pct"] - expected_pnl) < 0.01


class TestEmergencyCloseFailurePropagation:
    """C3: emergency_close must raise when all retries fail, not silently return."""

    @patch('luckytrader.execute.log_trade')
    @patch('luckytrader.execute.notify_discord')
    def test_emergency_close_raises_on_all_retries_failed(self, mock_notify, mock_log, mock_hl):
        """If all retries fail, emergency_close must raise RuntimeError."""
        from luckytrader.execute import emergency_close

        mock_hl.place_market_order.return_value = {"status": "err", "response": "Exchange down"}

        with pytest.raises(RuntimeError, match="紧急平仓失败"):
            emergency_close("BTC", 0.001, True, max_retries=2)

    @patch('luckytrader.execute.log_trade')
    def test_emergency_close_success_does_not_raise(self, mock_log, mock_hl):
        """Successful emergency close should NOT raise."""
        from luckytrader.execute import emergency_close

        mock_hl.place_market_order.return_value = {"status": "ok"}

        with patch('luckytrader.execute.save_state'):
            # Should not raise
            emergency_close("BTC", 0.001, True)

    @patch('luckytrader.execute.notify_discord')
    @patch('luckytrader.execute.get_coin_info', return_value={"szDecimals": 5})
    @patch('luckytrader.execute.log_trade')
    def test_open_position_catches_emergency_close_failure(self, mock_log, mock_coin, mock_notify, mock_hl):
        """open_position should handle emergency_close failure gracefully."""
        from luckytrader.execute import open_position

        mock_hl.get_account_info.return_value = {"account_value": "217.76"}
        mock_hl.place_market_order.side_effect = [
            {"status": "ok"},  # initial open succeeds
            {"status": "err"},  # emergency close attempt 1
            {"status": "err"},  # emergency close attempt 2
            {"status": "err"},  # emergency close attempt 3
        ]
        mock_hl.place_stop_loss.return_value = {"status": "err", "response": "failed"}

        with patch('luckytrader.execute.get_position') as mock_pos:
            mock_pos.return_value = {
                "coin": "BTC", "size": 0.001, "direction": "LONG",
                "entry_price": 67000.0, "unrealized_pnl": 0,
                "liquidation_price": 0,
            }
            with patch('luckytrader.execute.save_state'):
                result = open_position("LONG", {"price": 67000, "signal_reasons": []})

        # Should indicate the critical failure state
        assert result["action"] == "EMERGENCY_CLOSE_FAILED"

    @patch('luckytrader.execute.notify_discord')
    @patch('luckytrader.execute.get_coin_info', return_value={"szDecimals": 5})
    @patch('luckytrader.execute.log_trade')
    def test_tp_failure_triggers_emergency_close_failed(self, mock_log, mock_coin, mock_notify, mock_hl):
        """TP failure path: SL succeeds, TP fails, emergency_close fails → EMERGENCY_CLOSE_FAILED."""
        from luckytrader.execute import open_position

        mock_hl.get_account_info.return_value = {"account_value": "217.76"}
        mock_hl.place_market_order.side_effect = [
            {"status": "ok"},  # initial open succeeds
            {"status": "err"},  # emergency close attempt 1
            {"status": "err"},  # emergency close attempt 2
            {"status": "err"},  # emergency close attempt 3
        ]
        mock_hl.place_stop_loss.return_value = {"status": "ok"}
        mock_hl.place_take_profit.return_value = {"status": "err", "response": "failed"}
        mock_hl.cancel_order.return_value = {"status": "ok"}

        with patch('luckytrader.execute.get_position') as mock_pos, \
             patch('luckytrader.execute.get_open_orders_detailed', return_value=[]):
            mock_pos.return_value = {
                "coin": "BTC", "size": 0.001, "direction": "LONG",
                "entry_price": 67000.0, "unrealized_pnl": 0,
                "liquidation_price": 0,
            }
            with patch('luckytrader.execute.save_state'):
                result = open_position("LONG", {"price": 67000, "signal_reasons": []})

        assert result["action"] == "EMERGENCY_CLOSE_FAILED"


class TestFixSlTpEmergencyCloseFailure:
    """fix_sl_tp must not crash when emergency_close raises RuntimeError."""

    @patch('luckytrader.execute.notify_discord')
    @patch('luckytrader.execute.log_trade')
    def test_fix_sl_tp_survives_emergency_close_failure(self, mock_log, mock_notify, mock_hl):
        """fix_sl_tp should swallow RuntimeError from emergency_close."""
        from luckytrader.execute import fix_sl_tp

        mock_hl.place_stop_loss.side_effect = Exception("exchange down")
        mock_hl.place_market_order.return_value = {"status": "err"}  # emergency_close fails

        position = {
            "coin": "BTC", "size": 0.001, "direction": "LONG",
            "entry_price": 67000.0,
        }

        with patch('luckytrader.execute.check_sl_tp_orders', return_value=(False, True)), \
             patch('luckytrader.execute.save_state'):
            # Should NOT raise — RuntimeError is caught internally
            fix_sl_tp(position)


class TestClosePositionChecksResult:
    """close_position must not clear state if the market order fails."""

    @patch('luckytrader.execute.log_trade')
    def test_close_position_does_not_clear_state_on_failure(self, mock_log, mock_hl):
        """If place_market_order returns err, state must NOT be cleared."""
        from luckytrader.execute import close_position, load_state

        mock_hl.place_market_order.return_value = {"status": "err", "response": "exchange down"}
        mock_hl.get_open_orders_detailed.return_value = []

        position = {
            "coin": "BTC", "size": 0.001, "direction": "LONG",
            "entry_price": 67000.0,
        }

        with patch('luckytrader.execute.save_state') as mock_save:
            with pytest.raises(RuntimeError, match="平仓失败"):
                close_position(position)
            # save_state should NOT have been called with position=None
            for call_args in mock_save.call_args_list:
                state_arg = call_args[0][0]
                assert state_arg.get("position") is not None, \
                    "State was cleared despite failed close — position would be orphaned!"

    @patch('luckytrader.execute.log_trade')
    def test_close_position_clears_state_on_success(self, mock_log, mock_hl):
        """Successful close should clear state as before."""
        from luckytrader.execute import close_position

        mock_hl.place_market_order.return_value = {"status": "ok"}
        mock_hl.get_open_orders_detailed.return_value = []
        mock_hl.get_market_price.return_value = 68000.0

        position = {
            "coin": "BTC", "size": 0.001, "direction": "LONG",
            "entry_price": 67000.0,
        }

        with patch('luckytrader.execute.save_state') as mock_save:
            close_position(position)
            mock_save.assert_called_once()
            assert mock_save.call_args[0][0]["position"] is None
