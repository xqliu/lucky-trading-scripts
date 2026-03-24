"""Tests for ws_monitor max_hold timeout check.

Ensures TradeExecutor.check_max_hold_timeout() correctly:
1. Closes positions that exceed max_hold_hours
2. Ignores positions within the limit
3. Cleans state if position already closed on-chain
4. Handles missing state gracefully
"""
import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest


@pytest.fixture
def trade_executor():
    """Create a TradeExecutor with mocked dependencies."""
    from luckytrader.ws_monitor import TradeExecutor
    te = TradeExecutor()
    return te


def _make_position_state(direction="LONG", entry_price=70000, hours_ago=72, coin="BTC"):
    """Helper: create a position state dict with entry_time hours_ago."""
    entry_time = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return {
        "position": {
            "direction": direction,
            "entry_price": entry_price,
            "size": 0.001,
            "entry_time": entry_time.isoformat(),
        }
    }


class TestMaxHoldTimeout:
    """Test suite for check_max_hold_timeout."""

    @pytest.mark.asyncio
    async def test_no_position_returns_none(self, trade_executor):
        """No position state → no action."""
        with patch("luckytrader.execute.load_state", return_value={}):
            result = await trade_executor.check_max_hold_timeout("BTC")
            assert result is None

    @pytest.mark.asyncio
    async def test_position_within_limit_returns_none(self, trade_executor):
        """Position held for 30h with 60h limit → no action."""
        state = _make_position_state(hours_ago=30)
        with patch("luckytrader.execute.load_state", return_value=state), \
             patch("luckytrader.execute._get_coin_params", return_value={"max_hold_hours": 60}):
            result = await trade_executor.check_max_hold_timeout("BTC")
            assert result is None

    @pytest.mark.asyncio
    async def test_position_at_exact_limit_triggers_close(self, trade_executor):
        """Position held for exactly max_hold_hours → should close."""
        state = _make_position_state(direction="SHORT", entry_price=70000, hours_ago=60)
        mock_position = {"size": -0.001, "entry_price": 70000}

        with patch("luckytrader.execute.load_state", return_value=state), \
             patch("luckytrader.execute._get_coin_params", return_value={"max_hold_hours": 60}), \
             patch("luckytrader.execute.get_position", return_value=mock_position), \
             patch("luckytrader.trade.get_market_price", return_value=69000), \
             patch("luckytrader.execute.close_and_cleanup") as mock_close:
            result = await trade_executor.check_max_hold_timeout("BTC")
            assert result == "TIMEOUT_CLOSED"
            mock_close.assert_called_once()
            call_kwargs = mock_close.call_args
            assert call_kwargs[1]["reason"] == "TIMEOUT"

    @pytest.mark.asyncio
    async def test_position_exceeded_limit_long(self, trade_executor):
        """LONG position held for 72h with 60h limit → close with correct PnL."""
        state = _make_position_state(direction="LONG", entry_price=70000, hours_ago=72)
        mock_position = {"size": 0.001, "entry_price": 70000}

        with patch("luckytrader.execute.load_state", return_value=state), \
             patch("luckytrader.execute._get_coin_params", return_value={"max_hold_hours": 60}), \
             patch("luckytrader.execute.get_position", return_value=mock_position), \
             patch("luckytrader.trade.get_market_price", return_value=71000), \
             patch("luckytrader.execute.close_and_cleanup") as mock_close:
            result = await trade_executor.check_max_hold_timeout("BTC")
            assert result == "TIMEOUT_CLOSED"
            # Verify it was called with is_long=True and correct size
            args, kwargs = mock_close.call_args
            assert args[0] == "BTC"  # coin
            assert args[1] is True   # is_long
            assert args[2] == 0.001  # size

    @pytest.mark.asyncio
    async def test_position_already_closed_onchain(self, trade_executor):
        """Position in state but not on-chain → clean state, return STATE_CLEANED."""
        state = _make_position_state(hours_ago=72)

        with patch("luckytrader.execute.load_state", return_value=state), \
             patch("luckytrader.execute._get_coin_params", return_value={"max_hold_hours": 60}), \
             patch("luckytrader.execute.get_position", return_value=None), \
             patch("luckytrader.execute.save_state") as mock_save:
            result = await trade_executor.check_max_hold_timeout("BTC")
            assert result == "STATE_CLEANED"
            mock_save.assert_called_once_with({"position": None}, "BTC")

    @pytest.mark.asyncio
    async def test_no_entry_time_returns_none(self, trade_executor):
        """Position state without entry_time → skip."""
        state = {"position": {"direction": "LONG", "entry_price": 70000}}
        with patch("luckytrader.execute.load_state", return_value=state):
            result = await trade_executor.check_max_hold_timeout("BTC")
            assert result is None

    @pytest.mark.asyncio
    async def test_per_coin_max_hold(self, trade_executor):
        """Each coin can have its own max_hold_hours."""
        # BTC has 48h limit, position at 50h → should close
        state = _make_position_state(hours_ago=50)
        mock_position = {"size": 0.001, "entry_price": 70000}

        with patch("luckytrader.execute.load_state", return_value=state), \
             patch("luckytrader.execute._get_coin_params", return_value={"max_hold_hours": 48}), \
             patch("luckytrader.execute.get_position", return_value=mock_position), \
             patch("luckytrader.trade.get_market_price", return_value=70500), \
             patch("luckytrader.execute.close_and_cleanup") as mock_close:
            result = await trade_executor.check_max_hold_timeout("BTC")
            assert result == "TIMEOUT_CLOSED"

    @pytest.mark.asyncio
    async def test_short_position_pnl_calculation(self, trade_executor):
        """SHORT position PnL: (entry - current) / entry * 100."""
        state = _make_position_state(direction="SHORT", entry_price=70000, hours_ago=65)
        mock_position = {"size": -0.001, "entry_price": 70000}

        with patch("luckytrader.execute.load_state", return_value=state), \
             patch("luckytrader.execute._get_coin_params", return_value={"max_hold_hours": 60}), \
             patch("luckytrader.execute.get_position", return_value=mock_position), \
             patch("luckytrader.trade.get_market_price", return_value=68000), \
             patch("luckytrader.execute.close_and_cleanup") as mock_close:
            result = await trade_executor.check_max_hold_timeout("BTC")
            assert result == "TIMEOUT_CLOSED"
            _, kwargs = mock_close.call_args
            # SHORT: (70000 - 68000) / 70000 * 100 = 2.857%
            assert abs(kwargs["pnl_pct"] - 2.857) < 0.01
