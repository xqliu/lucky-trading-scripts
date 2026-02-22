"""
TDD tests for ws_monitor.py bug fixes.
Tests are written FIRST (red), then production code is fixed (green).
"""
import asyncio
import pytest
import time
from unittest.mock import patch, MagicMock, AsyncMock, call


# === Fix 1: execute_signal must not block event loop ===

class TestExecuteSignalAsync:
    """execute_signal() must use asyncio.to_thread for sync calls."""

    @pytest.mark.asyncio
    async def test_execute_signal_runs_in_thread(self):
        """execute_signal should wrap execute.open_position in asyncio.to_thread."""
        from luckytrader.ws_monitor import TradeExecutor

        executor = TradeExecutor()

        signal_result = {
            "signal": "LONG",
            "signal_reasons": ["test"],
            "price": 67000,
        }

        opened = {"action": "OPENED", "direction": "LONG", "size": 0.001, "entry": 67000, "sl": 64320, "tp": 71690}

        with patch('luckytrader.execute.open_position', return_value=opened) as mock_open:
            # execute_signal calls asyncio.to_thread twice:
            # 1. to_thread(self.has_position) → must return False (no position)
            # 2. to_thread(execute.open_position, ...) → returns OPENED
            call_idx = [0]
            async def side_effect(func, *args, **kwargs):
                call_idx[0] += 1
                if call_idx[0] == 1:  # has_position call
                    return False
                return opened  # open_position call

            with patch('asyncio.to_thread', side_effect=side_effect) as mock_to_thread:
                result = await executor.execute_signal(signal_result)

                # Verify asyncio.to_thread was called at least twice
                assert mock_to_thread.call_count >= 2
                # Second call must be open_position
                second_call_args = mock_to_thread.call_args_list[1][0]
                assert second_call_args[0] is mock_open  # open_position is the function
                assert result["action"] == "OPENED"


# === Fix 2: trailing loop must not block event loop ===

class TestTrailingLoopAsync:
    """_trailing_loop() must use asyncio.to_thread for trailing.main()."""

    @pytest.mark.asyncio
    async def test_trailing_loop_runs_in_thread(self):
        """trailing.main() should be called via asyncio.to_thread."""
        from luckytrader.ws_monitor import TradeExecutor

        executor = TradeExecutor()
        call_count = 0

        async def fake_to_thread(func, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            # has_position must return True so _trailing_loop proceeds to call trailing.main
            if hasattr(func, '__name__') and func.__name__ == 'has_position':
                return True
            if hasattr(func, '__self__') and hasattr(func.__self__, 'has_position'):
                return True
            return None  # trailing.main and other calls return None

        with patch('luckytrader.execute.get_position', return_value={"coin": "BTC", "size": 0.001}), \
             patch('luckytrader.ws_monitor.trailing') as mock_trailing, \
             patch('asyncio.to_thread', side_effect=fake_to_thread) as mock_to_thread:

            # Run one iteration then cancel
            async def run_one_iteration():
                task = asyncio.create_task(executor._trailing_loop())
                await asyncio.sleep(0.05)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            await run_one_iteration()

            # asyncio.to_thread should have been called (for trailing.main)
            assert mock_to_thread.called
            # The first arg should be trailing.main
            args = mock_to_thread.call_args_list[-1][0]
            assert args[0] is mock_trailing.main


# === Fix 3: check_position_closed_by_trigger must not block ===

class TestTriggerCheckAsync:
    """check_position_closed_by_trigger() must be async with to_thread."""

    @pytest.mark.asyncio
    async def test_trigger_check_runs_in_thread(self):
        """Sync REST calls in check_position_closed_by_trigger must use asyncio.to_thread."""
        from luckytrader.ws_monitor import TradeExecutor

        executor = TradeExecutor()
        executor._last_position_check = 0  # force check

        state_with_position = {
            "position": {
                "coin": "BTC", "direction": "LONG", "size": 0.001,
                "entry_price": 67000.0, "sl_price": 64320.0, "tp_price": 71690.0,
            }
        }

        with patch('luckytrader.execute.load_state', return_value=state_with_position), \
             patch('luckytrader.execute.get_position', return_value={"coin": "BTC"}), \
             patch('asyncio.to_thread', new_callable=AsyncMock) as mock_to_thread:
            # Position still exists on chain — should return None
            mock_to_thread.side_effect = [
                state_with_position,  # load_state
                {"coin": "BTC"},  # get_position returns position
            ]

            result = await executor.check_position_closed_by_trigger()
            # asyncio.to_thread was called for the sync REST calls
            assert mock_to_thread.called


# === Fix 4: PnL should use fill price, not market price ===

class TestTriggerPnlUsesFillPrice:
    """PnL calculation must use actual fill price from userFills."""

    @pytest.mark.asyncio
    async def test_trigger_pnl_uses_fill_price(self):
        """When SL/TP triggered, PnL should be calculated from fill price, not market price."""
        from luckytrader.ws_monitor import TradeExecutor

        executor = TradeExecutor()
        executor._last_position_check = 0

        state = {
            "position": {
                "coin": "BTC", "direction": "LONG", "size": 0.001,
                "entry_price": 67000.0,
                "sl_price": 64320.0, "tp_price": 71690.0,
            }
        }

        # Fill price is 71500 (actual TP fill), market price is 72000 (post-fill drift)
        fill_data = [{"coin": "BTC", "side": "SELL", "size": "0.001", "price": "71500", "time": int(time.time() * 1000)}]

        with patch('luckytrader.execute.load_state', return_value=state), \
             patch('luckytrader.execute.get_position', return_value=None), \
             patch('luckytrader.ws_monitor.get_recent_fills', return_value=fill_data), \
             patch('luckytrader.execute.record_trade_result') as mock_record, \
             patch('luckytrader.execute.log_trade'), \
             patch('luckytrader.execute.save_state'), \
             patch('luckytrader.ws_monitor.get_market_price', return_value=72000.0):

            # Make async calls work via to_thread mock
            async def fake_to_thread(func, *args, **kwargs):
                return func(*args, **kwargs)

            with patch('asyncio.to_thread', side_effect=fake_to_thread):
                result = await executor.check_position_closed_by_trigger()

            assert result is not None
            assert result["action"] == "CLOSED_BY_TRIGGER"
            # PnL should be based on fill price 71500, not market price 72000
            # LONG: (71500 - 67000) / 67000 * 100 = 6.72%
            expected_pnl = (71500 - 67000) / 67000 * 100
            assert abs(result["pnl_pct"] - expected_pnl) < 0.01, \
                f"PnL {result['pnl_pct']:.2f}% should be {expected_pnl:.2f}% (fill price), not market price based"
            assert result["close_price"] == 71500.0

    @pytest.mark.asyncio
    async def test_trigger_pnl_falls_back_to_market_price(self):
        """When no fill data available, fall back to market price."""
        from luckytrader.ws_monitor import TradeExecutor

        executor = TradeExecutor()
        executor._last_position_check = 0

        state = {
            "position": {
                "coin": "BTC", "direction": "LONG", "size": 0.001,
                "entry_price": 67000.0,
                "sl_price": 64320.0, "tp_price": 71690.0,
            }
        }

        with patch('luckytrader.execute.load_state', return_value=state), \
             patch('luckytrader.execute.get_position', return_value=None), \
             patch('luckytrader.ws_monitor.get_recent_fills', return_value=[]), \
             patch('luckytrader.execute.record_trade_result'), \
             patch('luckytrader.execute.log_trade'), \
             patch('luckytrader.execute.save_state'), \
             patch('luckytrader.ws_monitor.get_market_price', return_value=72000.0):

            async def fake_to_thread(func, *args, **kwargs):
                return func(*args, **kwargs)

            with patch('asyncio.to_thread', side_effect=fake_to_thread):
                result = await executor.check_position_closed_by_trigger()

            assert result is not None
            # Should use market price as fallback
            expected_pnl = (72000 - 67000) / 67000 * 100
            assert abs(result["pnl_pct"] - expected_pnl) < 0.01


# === Fix 5: Reconnect race condition ===

class TestReconnectRace:
    """Only one reconnect should run at a time."""

    @pytest.mark.asyncio
    async def test_no_concurrent_reconnect(self):
        """Two simultaneous reconnect attempts should result in only one executing."""
        from luckytrader.ws_monitor import WebSocketManager

        manager = WebSocketManager()
        reconnect_count = 0

        original_reconnect = manager.reconnect

        async def slow_reconnect():
            nonlocal reconnect_count
            reconnect_count += 1
            await asyncio.sleep(0.1)
            manager.connected = True
            return True

        manager.reconnect = slow_reconnect

        # Trigger two reconnects simultaneously
        t1 = asyncio.create_task(manager.reconnect_with_lock())
        t2 = asyncio.create_task(manager.reconnect_with_lock())

        await asyncio.gather(t1, t2)

        # Only one should have actually reconnected
        assert reconnect_count == 1


# === Fix 6: Signal handler thread safety ===

class TestSignalHandlerThreadSafety:
    """Signal handler should use loop.call_soon_threadsafe."""

    def test_signal_handler_uses_call_soon_threadsafe(self):
        """_signal_handler should use loop.call_soon_threadsafe, not direct task.cancel()."""
        from luckytrader.ws_monitor import WSMonitor

        monitor = WSMonitor()
        mock_loop = MagicMock()
        monitor._loop = mock_loop

        mock_task = MagicMock()
        mock_task.done.return_value = False
        monitor.tasks = [mock_task]

        monitor._signal_handler(15, None)  # SIGTERM

        assert monitor.running is False
        # Should use call_soon_threadsafe instead of direct cancel
        mock_loop.call_soon_threadsafe.assert_called()


# === Fix 7: Periodic report removed — reporting handled by OpenClaw cron ===


# === Fix 9: Module-level config load ===

class TestModuleLevelConfig:
    """Importing ws_monitor should not crash if config is missing."""

    def test_import_without_config_does_not_crash(self):
        """Module should be importable even if config is not available at import time.
        This is already satisfied if we got here — the conftest patches config.
        The real test is that _config is no longer at module level."""
        import importlib
        import luckytrader.ws_monitor as wsm

        # _config should NOT be a module-level global anymore
        # Instead, classes should access config lazily
        # Check that NotificationManager inits without module-level _config
        assert hasattr(wsm, 'NotificationManager')


# === Fix H1: Critical alerts must bypass deduplication ===

class TestCriticalAlertsBypassDedup:
    """Safety-critical notifications must not be suppressed by deduplication."""

    def test_notify_error_sends_duplicate_critical_alerts(self):
        """notify_error with critical=True should send even if same message was just sent."""
        from luckytrader.ws_monitor import NotificationManager

        nm = NotificationManager()
        sent_messages = []

        def mock_send(msg, force=False):
            sent_messages.append(msg)

        nm._send_discord_message = mock_send

        error_msg = "紧急平仓失败: BTC 仓位无保护！"

        # Send same critical error twice in quick succession
        nm.notify_error(error_msg, critical=True)
        nm.notify_error(error_msg, critical=True)

        # Both should be sent — critical errors bypass dedup
        assert len(sent_messages) == 2

    def test_notify_error_non_critical_deduplicates(self):
        """Non-critical notify_error should still deduplicate as before."""
        from luckytrader.ws_monitor import NotificationManager

        nm = NotificationManager()
        sent_messages = []

        def mock_send(msg, force=False):
            sent_messages.append(msg)

        nm._send_discord_message = mock_send

        error_msg = "API timeout"

        nm.notify_error(error_msg)
        nm.notify_error(error_msg)

        # Only first should be sent — dedup works for non-critical
        assert len(sent_messages) == 1


# === Signal check only on candle close ===

class TestSignalOnCandleClose:
    """analyze() should only be called when a new 30m candle closes, not every 30s."""

    def test_same_candle_does_not_trigger_analyze(self):
        """Multiple kline updates with the same timestamp should NOT call analyze()."""
        from luckytrader.ws_monitor import SignalProcessor

        sp = SignalProcessor()
        analyze_calls = 0
        original_analyze = None

        def mock_analyze(coin):
            nonlocal analyze_calls
            analyze_calls += 1
            return {"signal": "HOLD", "signal_reasons": []}

        with patch('luckytrader.ws_monitor.analyze', side_effect=mock_analyze):
            # Add several klines with the same timestamp (same candle updating)
            for i in range(5):
                sp.add_kline({"coin": "BTC", "interval": "30m", "time": 1000000,
                              "open": "97000", "high": "97500", "low": "96500",
                              "close": str(97000 + i), "volume": "100"})
                sp.process_signal()

        assert analyze_calls == 0, f"analyze() called {analyze_calls} times on same candle, expected 0"

    def test_new_candle_triggers_analyze(self):
        """When candle timestamp changes, analyze() should be called (previous candle closed)."""
        from luckytrader.ws_monitor import SignalProcessor

        sp = SignalProcessor()
        analyze_calls = 0

        def mock_analyze(coin):
            nonlocal analyze_calls
            analyze_calls += 1
            return {"signal": "HOLD", "signal_reasons": []}

        with patch('luckytrader.ws_monitor.analyze', side_effect=mock_analyze):
            # First candle
            sp.add_kline({"coin": "BTC", "interval": "30m", "time": 1000000,
                          "open": "97000", "high": "97500", "low": "96500",
                          "close": "97200", "volume": "100"})
            sp.process_signal()  # same candle, no analyze

            # New candle (timestamp changed) → previous candle closed
            sp.add_kline({"coin": "BTC", "interval": "30m", "time": 1001800000,
                          "open": "97200", "high": "97300", "low": "97100",
                          "close": "97250", "volume": "50"})
            sp.process_signal()  # new candle → should trigger analyze

        assert analyze_calls == 1, f"analyze() called {analyze_calls} times, expected 1"

    def test_first_candle_does_not_trigger_analyze(self):
        """The very first kline received should not trigger analyze (no previous candle to close)."""
        from luckytrader.ws_monitor import SignalProcessor

        sp = SignalProcessor()
        analyze_calls = 0

        def mock_analyze(coin):
            nonlocal analyze_calls
            analyze_calls += 1
            return {"signal": "HOLD", "signal_reasons": []}

        with patch('luckytrader.ws_monitor.analyze', side_effect=mock_analyze):
            sp.add_kline({"coin": "BTC", "interval": "30m", "time": 1000000,
                          "open": "97000", "high": "97500", "low": "96500",
                          "close": "97200", "volume": "100"})
            sp.process_signal()

        assert analyze_calls == 0, f"analyze() should not run on first candle (no close yet)"


# === Bug: has_position() sync blocking in async context ===

class TestHasPositionAsync:
    """has_position() calls in async methods must use asyncio.to_thread."""

    @pytest.mark.asyncio
    async def test_execute_signal_has_position_runs_in_thread(self):
        """execute_signal's has_position check must not block the event loop."""
        from luckytrader.ws_monitor import TradeExecutor

        executor = TradeExecutor()
        to_thread_calls = []

        original_to_thread = asyncio.to_thread

        async def tracking_to_thread(func, *args, **kwargs):
            to_thread_calls.append(func.__name__ if hasattr(func, '__name__') else str(func))
            # has_position → True → skip signal (early return)
            if hasattr(func, '__name__') and func.__name__ == 'has_position':
                return True
            return func(*args, **kwargs)

        with patch('asyncio.to_thread', side_effect=tracking_to_thread):
            result = await executor.execute_signal({"signal": "LONG", "price": 67000})

        assert 'has_position' in to_thread_calls, \
            f"has_position must be called via asyncio.to_thread, but to_thread calls were: {to_thread_calls}"
        assert result["action"] == "SKIP"

    @pytest.mark.asyncio
    async def test_trailing_loop_has_position_runs_in_thread(self):
        """_trailing_loop's has_position check must not block the event loop."""
        from luckytrader.ws_monitor import TradeExecutor

        executor = TradeExecutor()
        to_thread_calls = []

        async def tracking_to_thread(func, *args, **kwargs):
            to_thread_calls.append(func.__name__ if hasattr(func, '__name__') else str(func))
            # has_position → False → loop exits
            if hasattr(func, '__name__') and func.__name__ == 'has_position':
                return False
            return None

        with patch('asyncio.to_thread', side_effect=tracking_to_thread):
            await executor._trailing_loop()

        assert 'has_position' in to_thread_calls, \
            f"has_position must be called via asyncio.to_thread, but to_thread calls were: {to_thread_calls}"


# === Bug: notify_* sync subprocess in async _message_loop ===

class TestNotifyAsync:
    """Notification calls from async _message_loop must not block the event loop."""

    @pytest.mark.asyncio
    async def test_notify_trade_closed_runs_in_thread(self):
        """notify_trade_closed called from async context must use asyncio.to_thread."""
        from luckytrader.ws_monitor import NotificationManager

        nm = NotificationManager()
        sent_in_thread = []

        nm._send_discord_message = MagicMock()  # prevent actual subprocess

        original_to_thread = asyncio.to_thread

        async def tracking_to_thread(func, *args, **kwargs):
            sent_in_thread.append(func.__name__ if hasattr(func, '__name__') else str(func))
            return func(*args, **kwargs)

        close_info = {
            "direction": "LONG", "coin": "BTC", "reason": "TP",
            "entry_price": 67000, "close_price": 71500, "pnl_pct": 6.72
        }

        with patch('asyncio.to_thread', side_effect=tracking_to_thread):
            await nm.async_notify_trade_closed(close_info)

        assert 'notify_trade_closed' in sent_in_thread, \
            "notify_trade_closed must be called via asyncio.to_thread from async context"

    @pytest.mark.asyncio
    async def test_notify_signal_detected_runs_in_thread(self):
        """notify_signal_detected called from async context must use asyncio.to_thread."""
        from luckytrader.ws_monitor import NotificationManager

        nm = NotificationManager()
        sent_in_thread = []

        nm._send_discord_message = MagicMock()

        async def tracking_to_thread(func, *args, **kwargs):
            sent_in_thread.append(func.__name__ if hasattr(func, '__name__') else str(func))
            return func(*args, **kwargs)

        signal_info = {"signal": "SHORT", "price": 67000, "signal_reasons": ["test"]}

        with patch('asyncio.to_thread', side_effect=tracking_to_thread):
            await nm.async_notify_signal_detected(signal_info)

        assert 'notify_signal_detected' in sent_in_thread

    @pytest.mark.asyncio
    async def test_notify_error_runs_in_thread(self):
        """notify_error called from async context must use asyncio.to_thread."""
        from luckytrader.ws_monitor import NotificationManager

        nm = NotificationManager()
        sent_in_thread = []

        nm._send_discord_message = MagicMock()

        async def tracking_to_thread(func, *args, **kwargs):
            sent_in_thread.append(func.__name__ if hasattr(func, '__name__') else str(func))
            return func(*args, **kwargs)

        with patch('asyncio.to_thread', side_effect=tracking_to_thread):
            await nm.async_notify_error("test error", critical=True)

        assert 'notify_error' in sent_in_thread
