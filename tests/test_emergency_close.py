"""
Tests for emergency_close retry logic — life-or-death for positions.

Key invariant: emergency_close must NEVER open a reverse position.
After any successful close, it must verify the position is gone before retrying.
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
        
        with patch('luckytrader.execute.save_state'), \
             patch('luckytrader.execute.get_position', return_value=None):
            emergency_close("BTC", 0.001, True)
        
        assert mock_hl.place_market_order.call_count == 1
    
    @patch('luckytrader.execute.log_trade')
    @patch('luckytrader.execute.notify_discord')
    def test_succeeds_second_try(self, mock_notify, mock_log, mock_hl):
        """Attempt 1 fails (timeout), attempt 2 checks position still exists, then closes."""
        from luckytrader.execute import emergency_close
        
        mock_hl.place_market_order.side_effect = [
            Exception("timeout"),
            {"status": "ok"},
        ]
        
        # Position still exists after attempt 1 failure, gone after attempt 2
        with patch('luckytrader.execute.save_state'), \
             patch('luckytrader.execute.get_position', side_effect=[
                 {"direction": "LONG", "size": 0.001},  # before attempt 2: still there
                 None,  # verify after attempt 2: gone
             ]):
            emergency_close("BTC", 0.001, True)
        
        assert mock_hl.place_market_order.call_count == 2
        mock_hl.place_market_order.side_effect = None
    
    @patch('luckytrader.execute.log_trade')
    @patch('luckytrader.execute.notify_discord')
    def test_position_already_closed_skips_retry(self, mock_notify, mock_log, mock_hl):
        """If attempt 1 throws 429 but position is gone on check → no retry, no reverse open."""
        from luckytrader.execute import emergency_close
        
        mock_hl.place_market_order.side_effect = Exception("429 rate limit")
        
        # Position gone when checked before attempt 2 (the 429 actually went through)
        with patch('luckytrader.execute.save_state') as mock_save, \
             patch('luckytrader.execute.get_position', return_value=None):
            emergency_close("BTC", 0.001, True)
        
        # Only 1 attempt — the retry saw position gone and stopped
        assert mock_hl.place_market_order.call_count == 1
        mock_save.assert_called()
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
            # Position always exists (never closes)
            with pytest.raises(RuntimeError, match="紧急平仓失败"), \
                 patch('luckytrader.execute.get_position',
                       return_value={"direction": "LONG", "size": 0.001}):
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
        """Order returns error status → retry (position still exists)."""
        from luckytrader.execute import emergency_close
        
        mock_hl.place_market_order.side_effect = [
            {"status": "err", "response": "rate limited"},
            {"status": "ok"},
        ]
        
        with patch('luckytrader.execute.save_state'), \
             patch('luckytrader.execute.get_position', side_effect=[
                 {"direction": "SHORT", "size": 0.001},  # before attempt 2: still there
                 None,  # verify after attempt 2: gone
             ]):
            emergency_close("BTC", 0.001, False)
        
        assert mock_hl.place_market_order.call_count == 2
        mock_hl.place_market_order.side_effect = None
