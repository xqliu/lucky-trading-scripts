"""Tests for Circuit Breaker in WSMonitor.

CB logic:
- Compares current real-time 1m close vs previous candle's final close
- Triggers IMMEDIATELY when threshold breached (not waiting for candle close)
- Only triggers ONCE per 1m candle
- On candle switch: previous candle's last real-time close becomes new reference
"""

import asyncio
import pytest
from unittest.mock import patch, MagicMock, AsyncMock


@pytest.fixture
def monitor():
    """Create a WSMonitor instance with mocked dependencies."""
    with patch("luckytrader.ws_monitor.WebSocketManager"), \
         patch("luckytrader.ws_monitor.NotificationManager") as mock_notif, \
         patch("luckytrader.ws_monitor.StateManager"), \
         patch("luckytrader.ws_monitor.TradeExecutor"):
        from luckytrader.ws_monitor import WSMonitor
        m = WSMonitor()
        m.notification_manager.async_send_notification = AsyncMock()
        m.notification_manager.async_notify_error = AsyncMock()
        return m


def make_kline(close: float, time_ms: int) -> dict:
    return {
        "coin": "BTC",
        "interval": "1m",
        "time": time_ms,
        "open": str(close),
        "high": str(close * 1.001),
        "low": str(close * 0.999),
        "close": str(close),
        "volume": "100",
    }


SHORT_POS = {"direction": "SHORT", "entry_price": 70000, "size": -0.001, "coin": "BTC"}
LONG_POS = {"direction": "LONG", "entry_price": 70000, "size": 0.001, "coin": "BTC"}


class TestCBRealTimeTrigger:
    """CB should trigger in real-time within a candle, not wait for close."""

    @pytest.mark.asyncio
    async def test_triggers_mid_candle(self, monitor):
        """Spike within a candle should trigger immediately."""
        with patch("luckytrader.ws_monitor.execute") as mock_exec:
            mock_exec.get_position.return_value = SHORT_POS
            mock_exec.close_position.return_value = {"action": "CLOSED", "exit_price": 70400}

            # Candle 1: establish reference
            await monitor._check_circuit_breaker(make_kline(70000, 1000))
            # Candle 2 starts
            await monitor._check_circuit_breaker(make_kline(70100, 2000))
            # Still candle 2, price spikes > 0.5% vs candle 1 close (70000)
            await monitor._check_circuit_breaker(make_kline(70400, 2000))

            mock_exec.close_position.assert_called_once()

    @pytest.mark.asyncio
    async def test_first_update_establishes_reference(self, monitor):
        """Very first update establishes reference but doesn't trigger (no comparison yet)."""
        with patch("luckytrader.ws_monitor.execute") as mock_exec:
            # Single update — just records, no comparison possible
            await monitor._check_circuit_breaker(make_kline(70000, 1000))
            mock_exec.get_position.assert_not_called()
            # Second update in same candle CAN trigger (compares vs first)
            mock_exec.get_position.return_value = SHORT_POS
            mock_exec.close_position.return_value = {"action": "CLOSED", "exit_price": 71000}
            await monitor._check_circuit_breaker(make_kline(71000, 1000))  # +1.4%
            mock_exec.close_position.assert_called_once()  # correctly triggers


class TestCBOncePerCandle:
    """CB should only trigger once per 1m candle."""

    @pytest.mark.asyncio
    async def test_only_triggers_once(self, monitor):
        """After triggering, further updates in same candle should not re-trigger."""
        with patch("luckytrader.ws_monitor.execute") as mock_exec:
            mock_exec.get_position.return_value = SHORT_POS
            mock_exec.close_position.return_value = {"action": "CLOSED", "exit_price": 70400}

            # Establish reference
            await monitor._check_circuit_breaker(make_kline(70000, 1000))
            # New candle, spike
            await monitor._check_circuit_breaker(make_kline(70400, 2000))
            assert mock_exec.close_position.call_count == 1
            # More updates in same candle — should NOT trigger again
            await monitor._check_circuit_breaker(make_kline(70600, 2000))
            await monitor._check_circuit_breaker(make_kline(70800, 2000))
            assert mock_exec.close_position.call_count == 1

    @pytest.mark.asyncio
    async def test_new_candle_resets_trigger(self, monitor):
        """New candle should allow triggering again."""
        with patch("luckytrader.ws_monitor.execute") as mock_exec:
            mock_exec.get_position.side_effect = [
                SHORT_POS,  # first trigger
                SHORT_POS,  # second trigger (new candle)
            ]
            mock_exec.close_position.return_value = {"action": "CLOSED", "exit_price": 70400}

            # Candle 1: reference
            await monitor._check_circuit_breaker(make_kline(70000, 1000))
            # Candle 2: trigger
            await monitor._check_circuit_breaker(make_kline(70400, 2000))
            assert mock_exec.close_position.call_count == 1
            # Candle 3: reference updates to candle 2's last close (70400)
            # Spike again from 70400 base
            await monitor._check_circuit_breaker(make_kline(70800, 3000))  # +0.57% vs 70400
            assert mock_exec.close_position.call_count == 2


class TestCBReferenceUpdate:
    """Test that reference close updates correctly on candle switch."""

    @pytest.mark.asyncio
    async def test_reference_uses_last_realtime_close(self, monitor):
        """When candle switches, reference should be last real-time close, not first."""
        with patch("luckytrader.ws_monitor.execute") as mock_exec:
            mock_exec.get_position.return_value = SHORT_POS

            # Candle 1: opens at 70000, drifts to 70200
            await monitor._check_circuit_breaker(make_kline(70000, 1000))
            await monitor._check_circuit_breaker(make_kline(70100, 1000))
            await monitor._check_circuit_breaker(make_kline(70200, 1000))
            # Candle 2: reference should be 70200 (last update of candle 1)
            # 70500 vs 70200 = +0.43% — below threshold
            await monitor._check_circuit_breaker(make_kline(70500, 2000))
            mock_exec.close_position.assert_not_called()

            # 70560 vs 70200 = +0.51% — above threshold
            mock_exec.close_position.return_value = {"action": "CLOSED", "exit_price": 70560}
            await monitor._check_circuit_breaker(make_kline(70560, 2000))
            mock_exec.close_position.assert_called_once()


class TestCBAdverseDetection:
    """Test adverse vs favorable spike detection."""

    @pytest.mark.asyncio
    async def test_up_spike_adverse_to_short(self, monitor):
        """Up spike should be adverse to SHORT position."""
        with patch("luckytrader.ws_monitor.execute") as mock_exec:
            mock_exec.get_position.return_value = SHORT_POS
            mock_exec.close_position.return_value = {"action": "CLOSED", "exit_price": 70400}

            await monitor._check_circuit_breaker(make_kline(70000, 1000))
            await monitor._check_circuit_breaker(make_kline(70400, 2000))  # +0.57%
            mock_exec.close_position.assert_called_once()

    @pytest.mark.asyncio
    async def test_down_spike_adverse_to_long(self, monitor):
        """Down spike should be adverse to LONG position."""
        with patch("luckytrader.ws_monitor.execute") as mock_exec:
            mock_exec.get_position.return_value = LONG_POS
            mock_exec.close_position.return_value = {"action": "CLOSED", "exit_price": 69600}

            await monitor._check_circuit_breaker(make_kline(70000, 1000))
            await monitor._check_circuit_breaker(make_kline(69600, 2000))  # -0.57%
            mock_exec.close_position.assert_called_once()

    @pytest.mark.asyncio
    async def test_favorable_spike_no_close(self, monitor):
        """Favorable spike should NOT close position."""
        with patch("luckytrader.ws_monitor.execute") as mock_exec:
            mock_exec.get_position.return_value = LONG_POS

            await monitor._check_circuit_breaker(make_kline(70000, 1000))
            await monitor._check_circuit_breaker(make_kline(70400, 2000))  # UP for LONG = good
            mock_exec.close_position.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_position_no_close(self, monitor):
        """No position → spike ignored."""
        with patch("luckytrader.ws_monitor.execute") as mock_exec:
            mock_exec.get_position.return_value = None

            await monitor._check_circuit_breaker(make_kline(70000, 1000))
            await monitor._check_circuit_breaker(make_kline(70400, 2000))
            mock_exec.close_position.assert_not_called()


class TestCBThreshold:
    """Test threshold sensitivity."""

    @pytest.mark.asyncio
    async def test_below_threshold_no_trigger(self, monitor):
        """Change below 0.5% should not trigger."""
        with patch("luckytrader.ws_monitor.execute") as mock_exec:
            mock_exec.get_position.return_value = SHORT_POS

            await monitor._check_circuit_breaker(make_kline(70000, 1000))
            await monitor._check_circuit_breaker(make_kline(70300, 2000))  # +0.43%
            mock_exec.close_position.assert_not_called()

    @pytest.mark.asyncio
    async def test_at_threshold_triggers(self, monitor):
        """Change at exactly 0.5% should trigger."""
        with patch("luckytrader.ws_monitor.execute") as mock_exec:
            mock_exec.get_position.return_value = SHORT_POS
            mock_exec.close_position.return_value = {"action": "CLOSED", "exit_price": 70350}

            await monitor._check_circuit_breaker(make_kline(70000, 1000))
            await monitor._check_circuit_breaker(make_kline(70350, 2000))  # exactly +0.5%
            mock_exec.close_position.assert_called_once()


class TestCBPnLCalculation:
    """Test PnL calculation."""

    @pytest.mark.asyncio
    async def test_short_loss_pnl(self, monitor):
        """SHORT closed at higher price = loss."""
        with patch("luckytrader.ws_monitor.execute") as mock_exec:
            mock_exec.get_position.return_value = SHORT_POS
            mock_exec.close_position.return_value = {"action": "CLOSED", "exit_price": 70400}

            await monitor._check_circuit_breaker(make_kline(70000, 1000))
            await monitor._check_circuit_breaker(make_kline(70400, 2000))

            msg = monitor.notification_manager.async_send_notification.call_args[0][0]
            assert "Circuit Breaker" in msg
            assert "SHORT" in msg
            # entry 70000, exit 70400, size 0.001 → pnl_usd = 0.001 * (70000-70400) = -$0.40
            assert "-$0.40" in msg or "-0.40" in msg

    @pytest.mark.asyncio
    async def test_long_loss_pnl(self, monitor):
        """LONG closed at lower price = loss."""
        with patch("luckytrader.ws_monitor.execute") as mock_exec:
            mock_exec.get_position.return_value = LONG_POS
            mock_exec.close_position.return_value = {"action": "CLOSED", "exit_price": 69600}

            await monitor._check_circuit_breaker(make_kline(70000, 1000))
            await monitor._check_circuit_breaker(make_kline(69600, 2000))

            msg = monitor.notification_manager.async_send_notification.call_args[0][0]
            assert "LONG" in msg

    @pytest.mark.asyncio
    async def test_close_failure_notifies_error(self, monitor):
        """Failed close should send error notification."""
        with patch("luckytrader.ws_monitor.execute") as mock_exec:
            mock_exec.get_position.return_value = SHORT_POS
            mock_exec.close_position.return_value = {"action": "ERROR", "error": "timeout"}

            await monitor._check_circuit_breaker(make_kline(70000, 1000))
            await monitor._check_circuit_breaker(make_kline(70400, 2000))

            monitor.notification_manager.async_notify_error.assert_called_once()


class TestCBEdgeCases:
    """Edge cases."""

    @pytest.mark.asyncio
    async def test_exception_handled(self, monitor):
        """Exception in get_position should not crash."""
        with patch("luckytrader.ws_monitor.execute") as mock_exec:
            mock_exec.get_position.side_effect = Exception("API error")

            await monitor._check_circuit_breaker(make_kline(70000, 1000))
            await monitor._check_circuit_breaker(make_kline(70400, 2000))
            # Should not crash — just logged

    @pytest.mark.asyncio
    async def test_rapid_spikes_only_close_once(self, monitor):
        """After CB closes, next spike in same candle should not re-close."""
        with patch("luckytrader.ws_monitor.execute") as mock_exec:
            mock_exec.get_position.return_value = SHORT_POS
            mock_exec.close_position.return_value = {"action": "CLOSED", "exit_price": 70400}

            await monitor._check_circuit_breaker(make_kline(70000, 1000))
            # Multiple spikes in same candle
            await monitor._check_circuit_breaker(make_kline(70400, 2000))
            await monitor._check_circuit_breaker(make_kline(70600, 2000))
            await monitor._check_circuit_breaker(make_kline(71000, 2000))

            assert mock_exec.close_position.call_count == 1
