"""TDD tests for code review findings (Rounds 1-7).

Tests written FIRST (should FAIL), then code fixed to make them PASS.
"""
import json
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from datetime import datetime, timezone
from pathlib import Path


# ===== Round 2: pnl_usd and fees_usd must not be 0 =====

class TestPnlUsdCalculation:
    """Round 2: _record_closed_position must compute real pnl_usd and fees_usd."""

    def _make_eth_executor(self, pos, fills_fee=-0.05):
        from okx_bb.executor import BBExecutor
        with patch.object(BBExecutor, '__init__', lambda self: None):
            ex = BBExecutor()

        cfg = MagicMock()
        cfg.coin = "ETH"
        cfg.instId = "ETH-USDT-SWAP"
        cfg.fees = MagicMock()
        cfg.fees.taker_fee = 0.0005
        cfg.risk = MagicMock()
        cfg.risk.max_hold_bars = 96
        ex.cfg = cfg
        ex.instId = cfg.instId

        client = MagicMock()
        client.get_fills.return_value = [{
            "fillPx": str(pos.get("exit_price_expected", 2100)),
            "fillSz": str(pos["size"]),
            "fee": str(fills_fee),
            "ts": "1711152000000",
        }]
        client.get_ticker.return_value = {"last": pos.get("exit_price_expected", 2100)}
        client.get_order_detail.return_value = None
        ex.client = client
        return ex

    def _make_sol_executor(self, pos, fills_fee=-0.022):
        from okx_sol_bb.executor import SolBBExecutor
        with patch.object(SolBBExecutor, '__init__', lambda self: None):
            ex = SolBBExecutor()

        cfg = MagicMock()
        cfg.coin = "SOL"
        cfg.instId = "SOL-USDT-SWAP"
        cfg.fees = MagicMock()
        cfg.fees.taker_fee = 0.0005
        cfg.risk = MagicMock()
        cfg.risk.max_hold_bars = 96
        ex.cfg = cfg
        ex.instId = cfg.instId

        client = MagicMock()
        client.get_fills.return_value = [{
            "fillPx": str(pos.get("exit_price_expected", 145)),
            "fillSz": str(pos["size"]),
            "fee": str(fills_fee),
            "ts": "1711152000000",
        }]
        client.get_ticker.return_value = {"last": pos.get("exit_price_expected", 145)}
        client.get_order_detail.return_value = None
        ex.client = client
        return ex

    def test_eth_pnl_usd_nonzero(self, tmp_path):
        """ETH: pnl_usd must reflect actual USD profit/loss, not 0."""
        pos = {
            "direction": "LONG", "entry_price": 2000.0,
            "exit_price_expected": 2100.0, "size": 0.41,
            "entry_time": "2026-03-20T10:00:00+00:00",
            "sl_price": 1900.0, "tp_price": 2100.0,
        }
        ex = self._make_eth_executor(pos, fills_fee=-0.043)
        log_path = tmp_path / "trade_log.json"
        log_path.write_text("[]")

        with patch("okx_bb.executor.TRADE_LOG_FILE", log_path):
            result = ex._record_closed_position(pos, "tp")

        # LONG: (2100 - 2000) * 0.41 * 0.1 = $4.10 (before fees)
        assert result.pnl_usd != 0, f"pnl_usd should not be 0, got {result.pnl_usd}"
        assert result.pnl_usd > 0, f"LONG profit should be positive, got {result.pnl_usd}"

    def test_eth_fees_usd_nonzero(self, tmp_path):
        """ETH: fees_usd must reflect actual trading fees, not 0."""
        pos = {
            "direction": "LONG", "entry_price": 2000.0,
            "exit_price_expected": 2100.0, "size": 0.41,
            "entry_time": "2026-03-20T10:00:00+00:00",
            "sl_price": 1900.0, "tp_price": 2100.0,
        }
        ex = self._make_eth_executor(pos, fills_fee=-0.043)
        log_path = tmp_path / "trade_log.json"
        log_path.write_text("[]")

        with patch("okx_bb.executor.TRADE_LOG_FILE", log_path):
            result = ex._record_closed_position(pos, "tp")

        assert result.fees_usd != 0, f"fees_usd should not be 0, got {result.fees_usd}"
        assert result.fees_usd > 0, f"fees should be positive (cost), got {result.fees_usd}"

    def test_sol_pnl_usd_nonzero(self, tmp_path):
        """SOL: pnl_usd must reflect actual USD profit/loss, not 0."""
        pos = {
            "direction": "SHORT", "entry_price": 150.0,
            "exit_price_expected": 145.0, "size": 3.0,
            "entry_time": "2026-03-20T10:00:00+00:00",
            "sl_price": 158.0, "tp_price": 145.0,
        }
        ex = self._make_sol_executor(pos, fills_fee=-0.022)
        log_path = tmp_path / "trade_log.json"
        log_path.write_text("[]")

        with patch("okx_sol_bb.executor.TRADE_LOG_FILE", log_path):
            result = ex._record_closed_position(pos, "tp")

        # SHORT: (150 - 145) * 3.0 * 1.0 = $15.0 (before fees)
        assert result.pnl_usd != 0, f"pnl_usd should not be 0, got {result.pnl_usd}"
        assert result.pnl_usd > 0, f"SHORT profit should be positive, got {result.pnl_usd}"

    def test_trade_log_has_real_pnl_usd(self, tmp_path):
        """trade_log.json entry must have nonzero pnl_usd."""
        pos = {
            "direction": "LONG", "entry_price": 2000.0,
            "exit_price_expected": 2100.0, "size": 0.41,
            "entry_time": "2026-03-20T10:00:00+00:00",
            "sl_price": 1900.0, "tp_price": 2100.0,
        }
        ex = self._make_eth_executor(pos, fills_fee=-0.043)
        log_path = tmp_path / "trade_log.json"
        log_path.write_text("[]")

        with patch("okx_bb.executor.TRADE_LOG_FILE", log_path):
            ex._record_closed_position(pos, "tp")

        log = json.loads(log_path.read_text())
        assert len(log) == 1
        assert log[0]["pnl_usd"] != 0, "trade_log pnl_usd should not be 0"
        assert log[0]["fees_usd"] != 0, "trade_log fees_usd should not be 0"


# ===== Round 1/5: ctVal fail-safe =====

class TestCtValFailSafe:
    """Round 1/5: calculate_size must fail-safe when ctVal is missing."""

    def test_missing_ctval_not_use_wrong_fallback(self):
        """If instrument response lacks ctVal, must not silently use 0.01."""
        from okx_bb.executor import BBExecutor

        with patch.object(BBExecutor, '__init__', lambda self: None):
            ex = BBExecutor()

        cfg = MagicMock()
        cfg.instId = "ETH-USDT-SWAP"
        cfg.risk = MagicMock()
        cfg.risk.position_ratio = 0.95
        cfg.risk.stop_loss_pct = 0.05
        cfg.risk.max_single_loss = 50
        ex.cfg = cfg
        ex.instId = "ETH-USDT-SWAP"

        client = MagicMock()
        # Instrument WITHOUT ctVal key
        client.get_instrument.return_value = {
            "instId": "ETH-USDT-SWAP", "lotSz": "0.01", "minSz": "0.01"
        }
        client.get_ticker.return_value = {"last": 2000.0}
        client.get_balance.return_value = {"total_equity": 100.0}
        ex.client = client

        result = ex.calculate_size()
        # Should either return None (fail-safe) or use correct ctVal
        # Must NOT use fallback 0.01 (which would give 10x too many contracts)
        if result is not None:
            contracts = float(result)
            # With correct ctVal=0.1: ~95/(0.1*2000) = ~0.475 → 0.47
            # With wrong ctVal=0.01: ~95/(0.01*2000) = ~4.75 → 4.75 (10x too large!)
            assert contracts < 1.0, \
                f"contracts={contracts} — likely used wrong ctVal 0.01 fallback (should be ≤1.0 for $100 equity)"


# ===== Round 3: Discord notification must include USD PnL =====

class TestDiscordPnlNotification:
    """Round 3: Close notification must show USD PnL amount."""

    @pytest.mark.asyncio
    async def test_close_notification_contains_usd_pnl(self):
        """_check_position_closed notification must include dollar PnL."""
        from okx_bb.ws_monitor import WSMonitor
        from core.types import TradeResult, Direction, ExitReason

        with patch.object(WSMonitor, '__init__', lambda self: None):
            mon = WSMonitor()

        mon.executor = MagicMock()
        mon.cfg = MagicMock()
        mon.cfg.execution = MagicMock()
        mon.cfg.execution.mode = "close_confirm"

        fake_result = TradeResult(
            coin="ETH", direction=Direction.LONG,
            entry_price=2000.0, exit_price=2100.0,
            size=0.41, pnl_pct=0.049, pnl_usd=4.10,
            entry_time=datetime(2026, 3, 20, tzinfo=timezone.utc),
            exit_time=datetime(2026, 3, 21, tzinfo=timezone.utc),
            exit_reason=ExitReason.TP, fees_usd=0.086,
        )

        mon.executor.check_position = MagicMock(return_value=fake_result)

        async def fake_rest(fn, *args, **kwargs):
            return fn(*args, **kwargs)
        mon._rest = fake_rest

        captured_messages = []
        async def fake_send(msg, **kwargs):
            captured_messages.append(msg)

        with patch("okx_bb.ws_monitor.send_discord", side_effect=fake_send):
            await mon._check_position_closed()

        assert len(captured_messages) >= 1, "Should have sent a Discord message"
        msg = captured_messages[0]
        assert "$" in msg and ("4.1" in msg or "+4.1" in msg or "4.10" in msg), \
            f"Notification should contain USD PnL like '$4.10', got: {msg}"


# ===== Round 4: get_all_fills_history empty cursor protection =====

class TestFillsHistoryPagination:
    """Round 4: get_all_fills_history must handle empty billId."""

    def test_empty_billid_stops_pagination(self):
        """If fills have no billId, pagination must stop (not loop 50 times)."""
        from okx_bb.exchange import OKXClient

        with patch.object(OKXClient, '__init__', lambda self: None):
            client = OKXClient()

        call_count = 0
        def fake_get_fills_history(**kwargs):
            nonlocal call_count
            call_count += 1
            return [{"fillPx": "2000", "fillSz": "0.1"}] * 100

        client.get_fills_history = fake_get_fills_history
        result = client.get_all_fills_history(instId="ETH-USDT-SWAP")

        assert call_count <= 2, f"Should stop on empty billId, but called {call_count} times"


# ===== Round 6: ctVal stored in position_state =====

class TestCtValInPositionState:
    """Round 6: open_position should store ctVal in position_state."""

    def test_position_state_contains_ctval(self):
        """After opening a position, state should contain ct_val."""
        from okx_bb.executor import BBExecutor

        with patch.object(BBExecutor, '__init__', lambda self: None):
            ex = BBExecutor()

        cfg = MagicMock()
        cfg.coin = "ETH"
        cfg.instId = "ETH-USDT-SWAP"
        cfg.fees = MagicMock()
        cfg.fees.taker_fee = 0.0005
        cfg.fees.maker_fee = 0.0002
        cfg.risk = MagicMock()
        cfg.risk.position_ratio = 0.95
        cfg.risk.stop_loss_pct = 0.05
        cfg.risk.take_profit_pct = 0.02
        cfg.risk.max_hold_bars = 96
        cfg.risk.max_single_loss = 50
        cfg.execution = MagicMock()
        ex.cfg = cfg
        ex.instId = cfg.instId

        saved_states = []
        def fake_save(state):
            if state:
                saved_states.append(state.copy())
        ex.save_position = fake_save

        client = MagicMock()
        client.get_instrument.return_value = {
            "instId": "ETH-USDT-SWAP", "ctVal": "0.1",
            "lotSz": "0.01", "minSz": "0.01"
        }
        client.get_ticker.return_value = {"last": 2000.0}
        client.get_balance.return_value = {"total_equity": 100.0}
        client.get_positions.return_value = []  # no existing position
        client.place_market_order.return_value = {"code": "0", "data": [{"ordId": "123"}]}
        client.get_order_detail.return_value = {
            "state": "filled", "avgPx": "2000.0", "accFillSz": "0.41", "fillSz": "0.41"
        }
        client.place_stop_order.return_value = {"code": "0", "data": [{"algoId": "sl123"}]}
        client.get_algo_orders.return_value = [{"algoId": "sl123", "state": "live"}]
        client.place_limit_order.return_value = {"code": "0", "data": [{"ordId": "tp456"}]}
        ex.client = client
        ex.load_position = MagicMock(return_value=None)

        result = ex.open_position("LONG")

        assert len(saved_states) > 0, "open_position should have saved state"
        last_state = saved_states[-1]
        assert "ct_val" in last_state, \
            f"position_state should contain ct_val, got keys: {list(last_state.keys())}"
        assert float(last_state["ct_val"]) == 0.1
