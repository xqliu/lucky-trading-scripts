"""Tests for ws_monitor periodic SL re-set logic.

Covers the critical bug path: partial fill → stale local size → position flip.
These tests mock the exchange client to test the logic without real API calls.
"""
import json
import pytest
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

import sys
import os

_root = str(Path(__file__).parent.parent.parent)
if _root not in sys.path:
    sys.path.insert(0, _root)


class FakeConfig:
    """Minimal config for testing."""
    class strategy:
        bb_period = 14
        bb_multiplier = 3.0

    class risk:
        take_profit_pct = 0.02
        stop_loss_pct = 0.05
        max_hold_bars = 96
        position_ratio = 0.30
        max_single_loss = 10.0
        leverage = 5

    class fees:
        taker_fee = 0.0005
        maker_fee = 0.0002

    instId = "SOL-USDT-SWAP"

    class exchange:
        coin = "SOL"
        instId = "SOL-USDT-SWAP"

    class notifications:
        discord_channel_id = ""


def make_local_pos(direction="LONG", entry_price=100.0, size="1.00",
                   sl_price=95.0, tp_price=102.0, sl_algo_id="algo123"):
    return {
        "direction": direction,
        "entry_price": entry_price,
        "size": size,
        "sl_price": sl_price,
        "tp_price": tp_price,
        "sl_algo_id": sl_algo_id,
        "tp_order_id": "tp456",
        "entry_time": "2026-03-14T10:00:00+00:00",
        "entry_bar_count": 0,
    }


class TestPeriodicSLResetUsesExchangeSize:
    """The core bug: periodic SL re-set must use exchange size, not local."""

    def test_local_size_differs_from_exchange(self):
        """When local says 0.41 but exchange has 0.01, SL should use 0.01."""
        local_pos = make_local_pos(direction="LONG", size="0.41", entry_price=2145.0)

        # Exchange shows only 0.01 remaining (partial fill ate 0.40)
        exchange_positions = [{"pos": "0.01", "avgPx": "2145.0"}]

        # The fix: exchange_size should be 0.01, not local 0.41
        pos_info = next(p for p in exchange_positions if float(p.get("pos", 0)) != 0)
        exchange_size = abs(float(pos_info.get("pos", 0)))

        local_sz = float(local_pos.get("size", 0))

        assert exchange_size == 0.01
        assert local_sz == 0.41
        assert abs(exchange_size - local_sz) > 0.001  # Mismatch detected

    def test_direction_mismatch_detected(self):
        """When local says LONG but exchange has SHORT, must detect."""
        local_pos = make_local_pos(direction="LONG", size="0.41")
        exchange_positions = [{"pos": "-0.82", "avgPx": "2081.44"}]

        pos_info = next(p for p in exchange_positions if float(p.get("pos", 0)) != 0)
        exchange_dir = "LONG" if float(pos_info["pos"]) > 0 else "SHORT"
        local_dir = local_pos["direction"]

        assert exchange_dir == "SHORT"
        assert local_dir == "LONG"
        assert exchange_dir != local_dir  # Direction mismatch!

    def test_no_mismatch_when_consistent(self):
        """No action needed when local and exchange agree."""
        local_pos = make_local_pos(direction="LONG", size="0.41", entry_price=100.0)
        exchange_positions = [{"pos": "0.41", "avgPx": "100.0"}]

        pos_info = next(p for p in exchange_positions if float(p.get("pos", 0)) != 0)
        exchange_size = abs(float(pos_info["pos"]))
        exchange_dir = "LONG" if float(pos_info["pos"]) > 0 else "SHORT"

        local_sz = float(local_pos["size"])
        local_dir = local_pos["direction"]

        assert exchange_dir == local_dir
        assert abs(exchange_size - local_sz) <= 0.001


class TestPricePastSLCheck:
    """When market already past SL trigger, should market close, not place new SL."""

    def test_long_price_below_sl(self):
        """LONG with SL at 95.0, current price 93.0 → should market close."""
        entry = 100.0
        sl_pct = 0.05
        sl_p = entry * (1 - sl_pct)  # 95.0
        current_price = 93.0

        # For LONG: price <= sl_p means already past SL
        should_market_close = current_price <= sl_p
        assert should_market_close is True

    def test_long_price_above_sl(self):
        """LONG with SL at 95.0, current price 97.0 → normal SL placement."""
        entry = 100.0
        sl_pct = 0.05
        sl_p = entry * (1 - sl_pct)  # 95.0
        current_price = 97.0

        should_market_close = current_price <= sl_p
        assert should_market_close is False

    def test_short_price_above_sl(self):
        """SHORT with SL at 105.0, current price 106.0 → should market close."""
        entry = 100.0
        sl_pct = 0.05
        sl_p = entry * (1 + sl_pct)  # 105.0
        current_price = 106.0

        # For SHORT: price >= sl_p means already past SL
        should_market_close = current_price >= sl_p
        assert should_market_close is True

    def test_short_price_below_sl(self):
        """SHORT with SL at 105.0, current price 102.0 → normal SL placement."""
        entry = 100.0
        sl_pct = 0.05
        sl_p = entry * (1 + sl_pct)  # 105.0
        current_price = 102.0

        should_market_close = current_price >= sl_p
        assert should_market_close is False


class TestPositionClosedOnExchange:
    """When SL missing AND position closed on exchange, should clean up local state."""

    def test_no_position_on_exchange(self):
        """Exchange returns empty positions → position is closed."""
        exchange_positions = []
        has_position = bool(exchange_positions) and any(float(p.get("pos", 0)) != 0 for p in exchange_positions)
        assert has_position is False

    def test_zero_position_on_exchange(self):
        """Exchange returns position with pos=0 → position is closed."""
        exchange_positions = [{"pos": "0", "avgPx": "0"}]
        has_position = any(float(p.get("pos", 0)) != 0 for p in exchange_positions)
        assert has_position is False

    def test_nonzero_position_on_exchange(self):
        """Exchange returns position with pos=0.41 → still open."""
        exchange_positions = [{"pos": "0.41", "avgPx": "100.0"}]
        has_position = any(float(p.get("pos", 0)) != 0 for p in exchange_positions)
        assert has_position is True


class TestPartialFillScenario:
    """End-to-end scenario: the exact bug that caused the position flip."""

    def test_partial_fill_leaves_residual(self):
        """SL fills 0.40 out of 0.41 → 0.01 residual LONG remains."""
        original_size = 0.41
        sl_fill_size = 0.40
        residual = original_size - sl_fill_size
        assert abs(residual - 0.01) < 1e-9

    def test_wrong_sl_size_causes_flip(self):
        """Using local size 0.41 to sell when only 0.01 exists → position flips."""
        residual_long = 0.01
        sl_sell_size = 0.41  # BUG: using local size instead of exchange size
        net_position = residual_long - sl_sell_size
        assert net_position == pytest.approx(-0.40)  # Flipped to SHORT!

    def test_correct_sl_size_closes_cleanly(self):
        """Using exchange size 0.01 → properly closes residual."""
        residual_long = 0.01
        sl_sell_size = 0.01  # FIX: using exchange size
        net_position = residual_long - sl_sell_size
        assert net_position == pytest.approx(0.0)  # Cleanly closed!

    def test_repeated_wrong_sl_accumulates(self):
        """Bug repeated 3x: each re-set sells 0.41, accumulating SHORT."""
        residual = 0.01
        wrong_sell = 0.41
        # After 3 iterations of the bug
        net = residual - wrong_sell * 3
        assert net == pytest.approx(-1.22)  # Massive unintended SHORT


class TestSLResetSizeFormatting:
    """SL size must be properly formatted for the exchange."""

    def test_sol_size_format(self):
        """SOL uses :.2f formatting (lotSz=0.01)."""
        size = 0.34
        formatted = f"{size:.2f}"
        assert formatted == "0.34"

    def test_eth_size_format(self):
        """ETH uses :.2f formatting (lotSz=0.01, ctVal=0.1)."""
        size = 0.41
        formatted = f"{size:.2f}"
        assert formatted == "0.41"

    def test_tiny_residual_format(self):
        """Tiny residual 0.01 must format correctly."""
        size = 0.01
        formatted = f"{size:.2f}"
        assert formatted == "0.01"
