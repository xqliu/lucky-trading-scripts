"""
Tests for emergency_close retry logic — life-or-death for positions.
"""
import json
import pytest
from unittest.mock import patch, MagicMock, call
from pathlib import Path


class TestEmergencyCloseRetry:
    """Emergency close with retry and persistent alerting."""
    
    @patch('luckytrader.execute.log_trade')
    def test_succeeds_first_try(self, mock_log, mock_hl):
        from luckytrader.execute import emergency_close
        
        mock_hl.place_market_order.return_value = {"status": "ok"}
        
        with patch('luckytrader.execute.save_state'):
            emergency_close("BTC", 0.001, True)
        
        assert mock_hl.place_market_order.call_count == 1
    
    @patch('luckytrader.execute.log_trade')
    @patch('luckytrader.execute.notify_discord')
    def test_succeeds_second_try(self, mock_notify, mock_log, mock_hl):
        from luckytrader.execute import emergency_close
        
        mock_hl.place_market_order.side_effect = [
            Exception("timeout"),
            {"status": "ok"},
        ]
        
        with patch('luckytrader.execute.save_state'):
            emergency_close("BTC", 0.001, True)
        
        assert mock_hl.place_market_order.call_count == 2
        mock_hl.place_market_order.side_effect = None
    
    @patch('luckytrader.execute.notify_discord')
    def test_all_retries_fail_persists_danger(self, mock_notify, mock_hl, tmp_path):
        """All retries fail → DANGER file written + Discord alert."""
        from luckytrader import execute as execute_signal
        orig_workspace = execute_signal._WORKSPACE_DIR
        execute_signal._WORKSPACE_DIR = tmp_path
        
        mock_hl.place_market_order.side_effect = Exception("network down")
        
        try:
            from luckytrader.execute import emergency_close
            emergency_close("BTC", 0.001, True, max_retries=2)
            
            danger_file = tmp_path / "memory" / "trading" / "DANGER_UNPROTECTED.json"
            assert danger_file.exists()
            data = json.loads(danger_file.read_text())
            assert data["coin"] == "BTC"
            assert data["is_long"] == True
            
            mock_notify.assert_called_once()
            assert "紧急平仓失败" in mock_notify.call_args[0][0]
        finally:
            execute_signal._WORKSPACE_DIR = orig_workspace
            mock_hl.place_market_order.side_effect = None
    
    @patch('luckytrader.execute.log_trade')
    @patch('luckytrader.execute.notify_discord')
    def test_error_status_triggers_retry(self, mock_notify, mock_log, mock_hl):
        """Order returns error status → retry."""
        from luckytrader.execute import emergency_close
        
        mock_hl.place_market_order.side_effect = [
            {"status": "err", "response": "rate limited"},
            {"status": "ok"},
        ]
        
        with patch('luckytrader.execute.save_state'):
            emergency_close("BTC", 0.001, False)
        
        assert mock_hl.place_market_order.call_count == 2
        mock_hl.place_market_order.side_effect = None
