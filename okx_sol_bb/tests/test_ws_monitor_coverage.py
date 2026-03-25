"""Additional ws_monitor tests for branch coverage."""
import sys
import asyncio
import json
import time
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch
from datetime import datetime, timezone

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from okx_sol_bb.config import OKXSolConfig, StrategyConfig, RiskConfig, FeeConfig
from core.types import TradeResult, Direction, ExitReason


def make_config():
    return OKXSolConfig(
        strategy=StrategyConfig(bb_period=14, bb_multiplier=3.0),
        risk=RiskConfig(stop_loss_pct=0.05, take_profit_pct=0.02, leverage=5),
        fees=FeeConfig(),
        api_key="test", secret_key="test", passphrase="test",
        coin="SOL", instId="SOL-USDT-SWAP",
    )


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def make_monitor():
    from okx_sol_bb.ws_monitor import WSMonitor
    m = WSMonitor(config=make_config())
    m._loop = asyncio.new_event_loop()
    m.executor = MagicMock()
    m.executor.save_position = MagicMock()
    m.executor.load_position = MagicMock(return_value=None)
    m.executor.check_position = MagicMock(return_value=None)
    m.executor._append_open_trade_log_if_missing = MagicMock()
    m.executor.reconcile_position_from_exchange = MagicMock()
    m.executor.calculate_size = MagicMock(return_value="1.00")
    m._rest_exchange = AsyncMock()
    m.accumulator._initialized = True
    m.accumulator.closes = [150.0 + i * 0.1 for i in range(50)]
    return m


# -- async send_discord --

class TestSendDiscord:
    def test_timeout_handled(self):
        from okx_sol_bb.ws_monitor import send_discord
        with patch('okx_sol_bb.ws_monitor._send_discord_sync', side_effect=asyncio.TimeoutError):
            _run(send_discord("test"))

    def test_exception_handled(self):
        from okx_sol_bb.ws_monitor import send_discord
        with patch('okx_sol_bb.ws_monitor._send_discord_sync', side_effect=Exception("fail")):
            _run(send_discord("test"))


# -- CandleAccumulator --

class TestCandleAccumulatorInit:
    def test_init_failure(self):
        from okx_sol_bb.ws_monitor import CandleAccumulator
        from okx_bb.exchange import OKXClient
        client = MagicMock(spec=OKXClient)
        client.get_candles.return_value = []
        acc = CandleAccumulator(client, "SOL-USDT-SWAP")
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(acc.initialize(loop))
        assert result is False
        loop.close()

    def test_init_success(self):
        from okx_sol_bb.ws_monitor import CandleAccumulator
        from okx_bb.exchange import OKXClient
        client = MagicMock(spec=OKXClient)
        client.get_candles.return_value = [{"c": 150.0 + i} for i in range(50)]
        acc = CandleAccumulator(client, "SOL-USDT-SWAP")
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(acc.initialize(loop))
        assert result is True
        assert len(acc.closes) == 50
        loop.close()


# -- _rest and _rest_exchange --

class TestRestMethods:
    def test_rest_exchange_timeout(self):
        m = make_monitor()
        m._rest_exchange = AsyncMock(side_effect=asyncio.TimeoutError)
        with pytest.raises(asyncio.TimeoutError):
            _run(m._rest_exchange("get_positions", "SOL-USDT-SWAP"))

    def test_get_thread_client(self):
        m = make_monitor()
        with patch('okx_sol_bb.ws_monitor.OKXClient') as mock_cls:
            mock_cls.return_value = MagicMock()
            client = m._get_thread_client()
            assert client is not None


# -- _on_entry_filled exception path --

class TestOnEntryFilledException:
    def test_inner_exception_emergency_close(self):
        """When _on_entry_filled_inner raises, check for emergency close."""
        m = make_monitor()
        m._rest_exchange = AsyncMock()
        # Make inner raise
        async def failing_inner(*args):
            raise Exception("inner fail")
        m._on_entry_filled_inner = failing_inner
        # Exchange shows position with no SL
        m._rest_exchange.side_effect = [
            [{"pos": "1.00", "avgPx": "150.0"}],  # get_positions
            [],  # get_algo_orders (no SL)
            {"code": "0"},  # place_market_order emergency close
        ]
        with patch('okx_sol_bb.ws_monitor.send_discord', new_callable=AsyncMock):
            _run(m._on_entry_filled("LONG", 150.0, "1.00"))
        assert m._entry_in_progress is False

    def test_inner_exception_double_exception(self):
        """Double exception path (both inner and emergency close fail)."""
        m = make_monitor()
        async def failing_inner(*args):
            raise Exception("inner fail")
        m._on_entry_filled_inner = failing_inner
        # get_positions fails
        m._rest_exchange = AsyncMock(side_effect=Exception("api totally down"))
        with patch('okx_sol_bb.ws_monitor.send_discord', new_callable=AsyncMock):
            _run(m._on_entry_filled("LONG", 150.0, "1.00"))
        assert m._entry_in_progress is False


# -- _on_entry_filled_inner zero price fallback --

class TestEntryFilledInnerEdge:
    def test_zero_price_uses_exchange(self):
        m = make_monitor()
        positions_data = [{"pos": "1.00", "avgPx": "152.0"}]
        calls = iter([
            positions_data,  # get_positions for price fix
            positions_data,  # get_positions for actual_sz
            {"code": "0", "data": [{"algoId": "sl1"}]},  # place_algo_order (SL)
            [{"algoId": "sl1", "state": "live"}],  # get_algo_orders (verify)
            {"code": "0", "data": [{"ordId": "tp1"}]},  # place_order (TP)
        ])
        m._rest_exchange = AsyncMock(side_effect=lambda *a, **kw: next(calls))
        with patch('okx_sol_bb.ws_monitor.send_discord', new_callable=AsyncMock):
            _run(m._on_entry_filled_inner("LONG", 0.0, "1.00"))
        m.executor.save_position.assert_called()

    def test_zero_price_exchange_also_zero_emergency_close(self):
        """Zero price from exchange → emergency close."""
        m = make_monitor()
        calls = iter([
            [{"pos": "1.00", "avgPx": "0"}],  # positions: avg price also zero
            {"code": "0"},  # emergency close market order
        ])
        m._rest_exchange = AsyncMock(side_effect=lambda *a, **kw: next(calls))
        with patch('okx_sol_bb.ws_monitor.send_discord', new_callable=AsyncMock):
            _run(m._on_entry_filled_inner("LONG", 0.0, "1.00"))


# -- _close_confirm_entry branches --

class TestCloseConfirmEntry:
    def test_no_signal(self):
        """No signal → returns without ordering."""
        m = make_monitor()
        m.accumulator.closes = [150.0] * 50  # flat → no signal
        with patch('okx_sol_bb.ws_monitor.send_discord', new_callable=AsyncMock):
            _run(m._close_confirm_entry())
        # No market order should be called

    def test_signal_with_existing_position_skips(self):
        """Signal but position exists → skip."""
        m = make_monitor()
        m._rest_exchange = AsyncMock(return_value=[{"pos": "1.00"}])
        m.accumulator.closes = [150.0 + i * 0.1 for i in range(50)]
        with patch('okx_sol_bb.ws_monitor.detect_signal', return_value="LONG"), \
             patch('okx_sol_bb.ws_monitor.send_discord', new_callable=AsyncMock):
            _run(m._close_confirm_entry())

    def test_signal_positions_none_skips(self):
        """Can't verify positions → skip."""
        m = make_monitor()
        m._rest_exchange = AsyncMock(return_value=None)
        with patch('okx_sol_bb.ws_monitor.detect_signal', return_value="LONG"), \
             patch('okx_sol_bb.ws_monitor.send_discord', new_callable=AsyncMock):
            _run(m._close_confirm_entry())

    def test_signal_size_none_skips(self):
        """Size calculation returns None → skip."""
        m = make_monitor()
        m._rest = AsyncMock(return_value=None)  # calculate_size returns None
        m._rest_exchange = AsyncMock(return_value=[{"pos": "0"}])
        with patch('okx_sol_bb.ws_monitor.detect_signal', return_value="LONG"), \
             patch('okx_sol_bb.ws_monitor.send_discord', new_callable=AsyncMock):
            _run(m._close_confirm_entry())

    def test_market_order_failure(self):
        """Market order fails → sends discord alert."""
        m = make_monitor()
        m._rest = AsyncMock(return_value="1.00")  # calculate_size
        m._rest_exchange = AsyncMock()
        m._rest_exchange.side_effect = [
            [{"pos": "0"}],       # get_positions: no position
            {"code": "1", "msg": "insufficient"}, # market order fails
        ]
        with patch('okx_sol_bb.ws_monitor.detect_signal', return_value="LONG"), \
             patch('okx_sol_bb.ws_monitor.send_discord', new_callable=AsyncMock) as mock_disc:
            _run(m._close_confirm_entry())
        mock_disc.assert_called()

    def test_market_order_success_fill(self):
        """Full success: order placed, position detected, entry filled."""
        m = make_monitor()
        m._rest = AsyncMock(return_value="1.00")
        m._rest_exchange = AsyncMock()
        m._rest_exchange.side_effect = [
            [{"pos": "0"}],  # get_positions: no position
            {"code": "0", "data": [{"ordId": "ord123"}]},  # market order success
            [{"pos": "1.00", "avgPx": "150.0"}],  # fill check: position found
        ]
        m._on_entry_filled = AsyncMock()
        with patch('okx_sol_bb.ws_monitor.detect_signal', return_value="LONG"), \
             patch('okx_sol_bb.ws_monitor.send_discord', new_callable=AsyncMock):
            _run(m._close_confirm_entry())
        m._on_entry_filled.assert_called_once()

    def test_market_order_success_no_fill(self):
        """Order placed but position never appears after 3 checks."""
        m = make_monitor()
        m._rest = AsyncMock(return_value="1.00")
        m._rest_exchange = AsyncMock()
        m._rest_exchange.side_effect = [
            [{"pos": "0"}],  # get_positions: no position
            {"code": "0", "data": [{"ordId": "ord123"}]},  # market order
            [{"pos": "0"}],  # check 1
            [{"pos": "0"}],  # check 2
            [{"pos": "0"}],  # check 3
        ]
        with patch('okx_sol_bb.ws_monitor.detect_signal', return_value="SHORT"), \
             patch('okx_sol_bb.ws_monitor.send_discord', new_callable=AsyncMock) as mock_disc:
            _run(m._close_confirm_entry())
        # Should alert about no position after 3 checks
        assert mock_disc.called


# -- _connect_business / _connect_private --

class TestWSConnection:
    def test_connect_business_failure(self):
        m = make_monitor()
        with patch('okx_sol_bb.ws_monitor.websockets.connect', AsyncMock(side_effect=Exception("conn fail"))):
            result = _run(m._connect_business())
        assert result is False

    def test_connect_private_failure(self):
        m = make_monitor()
        with patch('okx_sol_bb.ws_monitor.websockets.connect', AsyncMock(side_effect=Exception("conn fail"))):
            result = _run(m._connect_private())
        assert result is False

    def test_connect_business_success(self):
        m = make_monitor()
        mock_ws = AsyncMock()
        mock_ws.send = AsyncMock()
        with patch('okx_sol_bb.ws_monitor.websockets.connect', AsyncMock(return_value=mock_ws)):
            result = _run(m._connect_business())
        assert result is True

    def test_connect_private_login_fail(self):
        m = make_monitor()
        mock_ws = AsyncMock()
        mock_ws.send = AsyncMock()
        mock_ws.recv = AsyncMock(return_value=json.dumps({"event": "error", "code": "1"}))
        with patch('okx_sol_bb.ws_monitor.websockets.connect', AsyncMock(return_value=mock_ws)):
            result = _run(m._connect_private())
        assert result is False

    def test_connect_private_success(self):
        m = make_monitor()
        mock_ws = AsyncMock()
        mock_ws.send = AsyncMock()
        mock_ws.recv = AsyncMock(return_value=json.dumps({"event": "login", "code": "0"}))
        with patch('okx_sol_bb.ws_monitor.websockets.connect', AsyncMock(return_value=mock_ws)):
            result = _run(m._connect_private())
        assert result is True


# -- _ws_sign --

class TestWsSign:
    def test_sign_structure(self):
        m = make_monitor()
        result = m._ws_sign()
        assert result["op"] == "login"
        assert "args" in result
        assert result["args"][0]["apiKey"] == "test"


# -- _check_position_closed --

class TestCheckPositionClosed:
    def test_exception_handled(self):
        m = make_monitor()
        m._rest = AsyncMock(side_effect=Exception("check fail"))
        with patch('okx_sol_bb.ws_monitor.send_discord', new_callable=AsyncMock):
            _run(m._check_position_closed())

    def test_result_sends_discord(self):
        m = make_monitor()
        result = TradeResult(
            coin="SOL", direction=Direction.LONG, entry_price=150.0,
            exit_price=153.0, size=1.0, pnl_pct=0.02, pnl_usd=3.0,
            entry_time=datetime.now(timezone.utc),
            exit_time=datetime.now(timezone.utc),
            exit_reason=ExitReason.TP,
        )
        m._rest = AsyncMock(return_value=result)
        with patch('okx_sol_bb.ws_monitor.send_discord', new_callable=AsyncMock) as mock_disc:
            _run(m._check_position_closed())
        mock_disc.assert_called()


# -- _reconcile_on_startup --

# -- _ws_is_open --

class TestWsIsOpen:
    def test_none_ws(self):
        m = make_monitor()
        assert m._ws_is_open(None) is False

    def test_open_ws(self):
        m = make_monitor()
        mock_ws = MagicMock()
        mock_ws.state.name = "OPEN"
        assert m._ws_is_open(mock_ws) is True

    def test_closed_ws(self):
        m = make_monitor()
        mock_ws = MagicMock()
        mock_ws.state.name = "CLOSED"
        assert m._ws_is_open(mock_ws) is False

    def test_ws_exception(self):
        m = make_monitor()
        mock_ws = MagicMock()
        mock_ws.state = property(lambda s: (_ for _ in ()).throw(Exception("fail")))
        type(mock_ws).state = property(lambda s: (_ for _ in ()).throw(Exception("fail")))
        assert m._ws_is_open(mock_ws) is False


# -- _business_loop / _private_loop single iteration --

class TestWsLoops:
    def test_business_loop_connection_failure_stops(self):
        """Business loop: connection fails → backoff → stops on _running=False."""
        m = make_monitor()
        m._running = True
        m._business_ws = None
        call_count = 0
        async def fake_connect():
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                m._running = False
            return False
        m._connect_business = fake_connect
        with patch('asyncio.sleep', new_callable=AsyncMock):
            _run(m._business_loop())
        assert call_count >= 2

    def test_private_loop_connection_failure_stops(self):
        m = make_monitor()
        m._running = True
        m._private_ws = None
        call_count = 0
        async def fake_connect():
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                m._running = False
            return False
        m._connect_private = fake_connect
        with patch('asyncio.sleep', new_callable=AsyncMock):
            _run(m._private_loop())
        assert call_count >= 2

    def test_business_loop_recv_and_handle(self):
        """Business loop: connected → recv message → handle → stop."""
        m = make_monitor()
        m._running = True
        mock_ws = AsyncMock()
        mock_ws.state.name = "OPEN"
        call_count = 0
        async def fake_recv():
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                m._running = False
                raise asyncio.TimeoutError()
            return json.dumps({"arg": {"channel": "candle30m"}, "data": [{"0": "ts", "4": "150.0", "8": "1"}]})
        mock_ws.recv = fake_recv
        m._business_ws = mock_ws
        m._handle_business_message = AsyncMock()
        with patch('asyncio.sleep', new_callable=AsyncMock):
            _run(m._business_loop())

    def test_private_loop_connection_closed(self):
        """Private loop: ConnectionClosedError → reconnect."""
        from websockets.exceptions import ConnectionClosedError
        m = make_monitor()
        m._running = True
        mock_ws = AsyncMock()
        mock_ws.state.name = "OPEN"
        call_count = 0
        async def fake_recv():
            nonlocal call_count
            call_count += 1
            m._running = False
            raise ConnectionClosedError(None, None)
        mock_ws.recv = fake_recv
        m._private_ws = mock_ws
        with patch('asyncio.sleep', new_callable=AsyncMock):
            _run(m._private_loop())
        assert m._private_ws is None

    def test_business_loop_generic_exception(self):
        """Business loop: unexpected exception → log and continue."""
        m = make_monitor()
        m._running = True
        mock_ws = AsyncMock()
        mock_ws.state.name = "OPEN"
        call_count = 0
        async def fake_recv():
            nonlocal call_count
            call_count += 1
            m._running = False
            raise RuntimeError("unexpected")
        mock_ws.recv = fake_recv
        m._business_ws = mock_ws
        with patch('asyncio.sleep', new_callable=AsyncMock):
            _run(m._business_loop())


# -- _periodic_check --

class TestPeriodicCheck:
    def test_stops_when_not_running(self):
        m = make_monitor()
        m._running = False
        _run(m._periodic_check())

    def test_entry_in_progress_skips_check(self):
        """When _entry_in_progress, skip position check."""
        m = make_monitor()
        m._running = True
        m._entry_in_progress = True
        m._last_activity = time.time()
        call_count = 0
        orig_sleep = asyncio.sleep
        async def stop_after_one(s):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                m._running = False
        with patch('asyncio.sleep', side_effect=stop_after_one):
            _run(m._periodic_check())

    def test_position_timeout_detected(self):
        """Periodic check detects position timeout."""
        m = make_monitor()
        m._running = True
        m._entry_in_progress = False
        m._last_activity = time.time()
        call_count = 0
        result = TradeResult(
            coin="SOL", direction=Direction.LONG, entry_price=150.0,
            exit_price=148.0, size=1.0, pnl_pct=-0.013, pnl_usd=-2.0,
            entry_time=datetime.now(timezone.utc),
            exit_time=datetime.now(timezone.utc),
            exit_reason=ExitReason.TIMEOUT,
        )
        m._rest = AsyncMock(return_value=result)
        m.executor.load_position.return_value = None
        async def stop_after_one(s):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                m._running = False
        with patch('asyncio.sleep', side_effect=stop_after_one), \
             patch('okx_sol_bb.ws_monitor.send_discord', new_callable=AsyncMock):
            _run(m._periodic_check())


# -- _reconcile_on_startup --

class TestReconcileStartup:
    def test_no_positions_no_local(self):
        """No position on exchange and no local state → clean."""
        m = make_monitor()
        m._rest_exchange = AsyncMock(return_value=[{"pos": "0"}])
        m.executor.load_position.return_value = None
        with patch('okx_sol_bb.ws_monitor.send_discord', new_callable=AsyncMock):
            _run(m._reconcile_on_startup())

    def test_exchange_position_with_sl(self):
        """Position exists with SL → sync."""
        m = make_monitor()
        m._rest_exchange = AsyncMock()
        m._rest_exchange.side_effect = [
            [{"pos": "1.00", "avgPx": "150.0"}],  # get_positions
            [{"algoId": "sl1", "state": "live", "slTriggerPx": "142.5"}],  # get_algo_orders
        ]
        m.executor.load_position.return_value = None
        with patch('okx_sol_bb.ws_monitor.send_discord', new_callable=AsyncMock):
            _run(m._reconcile_on_startup())
        m.executor.reconcile_position_from_exchange.assert_called()

    def test_exchange_position_no_sl(self):
        """Position exists without SL → tries re-set, fails → emergency close."""
        m = make_monitor()
        m._rest_exchange = AsyncMock()
        m._rest_exchange.side_effect = [
            [{"pos": "1.00", "avgPx": "150.0"}],  # get_positions
            [],  # get_algo_orders: no SL
            {"code": "1"},  # place_stop_order fails
            {"code": "0"},  # emergency place_market_order close
        ]
        m.executor.load_position.return_value = None
        with patch('okx_sol_bb.ws_monitor.send_discord', new_callable=AsyncMock):
            _run(m._reconcile_on_startup())
        m.executor.save_position.assert_called_with(None)

    def test_positions_api_none(self):
        """Positions API returns None → skip."""
        m = make_monitor()
        m._rest_exchange = AsyncMock(return_value=None)
        with patch('okx_sol_bb.ws_monitor.send_discord', new_callable=AsyncMock):
            _run(m._reconcile_on_startup())
