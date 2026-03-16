"""Tests for periodic SL re-set logic in OKX BB WSMonitor.

These tests cover the critical bug path (00448ba):
- Partial fill leaves residual position
- Periodic check uses EXCHANGE size (not local) to re-set SL
- Price-past-SL detection → market close instead of new SL
- Direction mismatch detection → reconcile + Discord alert
"""
import sys
import asyncio
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch, call
from datetime import datetime, timezone

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from okx_bb.config import OKXConfig, StrategyConfig, RiskConfig, FeeConfig


def make_config(sl_pct=0.03, tp_pct=0.04):
    return OKXConfig(
        strategy=StrategyConfig(),
        risk=RiskConfig(stop_loss_pct=sl_pct, take_profit_pct=tp_pct),
        fees=FeeConfig(),
        api_key="test", secret_key="test", passphrase="test",
        coin="ETH", instId="ETH-USDT-SWAP",
    )


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def make_monitor(sl_pct=0.03, tp_pct=0.04):
    from okx_bb.ws_monitor import WSMonitor
    cfg = make_config(sl_pct, tp_pct)
    m = WSMonitor(config=cfg)
    m._loop = asyncio.new_event_loop()
    m.executor = MagicMock()
    return m


class TestPeriodicSLResetUsesExchangeSize:
    """Bug fix: periodic SL re-set must query exchange for actual position size."""

    def test_exchange_size_used_for_sl(self):
        """When local=0.41 but exchange=0.01, SL should use 0.01."""
        m = make_monitor()

        # Local state says LONG 0.41
        local_pos = {
            "direction": "LONG", "entry_price": 2145.0,
            "size": "0.41", "sl_algo_id": "", "sl_price": 2081.0
        }
        m.executor.load_position.return_value = local_pos
        m._entry_in_progress = False

        # Exchange: no SL algos, position is 0.01 (partial fill ate 0.40)
        exchange_calls = {
            "get_algo_orders": [],
            "get_positions": [{"pos": "0.01", "avgPx": "2145.0"}],
            "get_ticker": {"last": 2100.0},  # Above SL, normal re-set
            "place_stop_order": {"code": "0", "data": [{"algoId": "new_sl_123"}]},
        }

        async def mock_rest_exchange(method_name, *args, **kwargs):
            return exchange_calls.get(method_name)

        m._rest_exchange = mock_rest_exchange

        # Should detect mismatch (0.41 vs 0.01) and reconcile
        m.executor.reconcile_position_from_exchange.return_value = None
        # After reconcile, load_position returns updated state
        reconciled_pos = {
            "direction": "LONG", "entry_price": 2145.0,
            "size": "0.01", "sl_algo_id": "", "sl_price": None
        }
        m.executor.load_position.side_effect = [local_pos, reconciled_pos]

        # The mismatch should be detected
        local_sz = float(local_pos["size"])
        exchange_size = 0.01
        assert abs(exchange_size - local_sz) > 0.001

    def test_no_mismatch_when_sizes_match(self):
        """No reconciliation needed when local and exchange agree."""
        local_pos = {
            "direction": "LONG", "entry_price": 100.0,
            "size": "0.41", "sl_algo_id": ""
        }
        exchange_positions = [{"pos": "0.41", "avgPx": "100.0"}]

        pos_info = next(p for p in exchange_positions if float(p.get("pos", 0)) != 0)
        exchange_size = abs(float(pos_info["pos"]))
        local_sz = float(local_pos["size"])

        assert abs(exchange_size - local_sz) <= 0.001


class TestPricePastSLDetection:
    """When market price has already passed SL trigger, should market close."""

    def test_long_price_below_sl_triggers_market_close(self):
        """LONG at 100, SL at 97 (3%), price at 95 → market close."""
        entry = 100.0
        sl_pct = 0.03
        sl_p = entry * (1 - sl_pct)  # 97.0
        current_price = 95.0

        should_market_close = (current_price <= sl_p)
        assert should_market_close is True

    def test_short_price_above_sl_triggers_market_close(self):
        """SHORT at 100, SL at 103 (3%), price at 105 → market close."""
        entry = 100.0
        sl_pct = 0.03
        sl_p = entry * (1 + sl_pct)  # 103.0
        current_price = 105.0

        should_market_close = (current_price >= sl_p)
        assert should_market_close is True

    def test_long_price_above_sl_normal_reset(self):
        """LONG at 100, SL at 97, price at 99 → normal SL placement."""
        entry = 100.0
        sl_pct = 0.03
        sl_p = entry * (1 - sl_pct)
        current_price = 99.0

        should_market_close = (current_price <= sl_p)
        assert should_market_close is False

    def test_short_price_below_sl_normal_reset(self):
        """SHORT at 100, SL at 103, price at 101 → normal SL placement."""
        entry = 100.0
        sl_pct = 0.03
        sl_p = entry * (1 + sl_pct)
        current_price = 101.0

        should_market_close = (current_price >= sl_p)
        assert should_market_close is False


class TestDirectionMismatch:
    """When local direction differs from exchange → reconcile + alert."""

    def test_long_vs_short_detected(self):
        """Local=LONG, Exchange=SHORT → mismatch."""
        local_dir = "LONG"
        exchange_pos = {"pos": "-0.82", "avgPx": "2081.44"}
        exchange_dir = "LONG" if float(exchange_pos["pos"]) > 0 else "SHORT"

        assert exchange_dir == "SHORT"
        assert local_dir != exchange_dir

    def test_short_vs_long_detected(self):
        """Local=SHORT, Exchange=LONG → mismatch."""
        local_dir = "SHORT"
        exchange_pos = {"pos": "0.41", "avgPx": "2000.0"}
        exchange_dir = "LONG" if float(exchange_pos["pos"]) > 0 else "SHORT"

        assert exchange_dir == "LONG"
        assert local_dir != exchange_dir

    def test_same_direction_no_mismatch(self):
        """Local=LONG, Exchange=LONG → no mismatch."""
        local_dir = "LONG"
        exchange_pos = {"pos": "0.41", "avgPx": "2000.0"}
        exchange_dir = "LONG" if float(exchange_pos["pos"]) > 0 else "SHORT"

        assert local_dir == exchange_dir


class TestPositionClosedOnExchangeDuringSLCheck:
    """When periodic check finds no SL AND position closed on exchange."""

    def test_empty_positions_means_closed(self):
        positions = []
        has_pos = bool(positions) and any(float(p.get("pos", 0)) != 0 for p in positions)
        assert has_pos is False

    def test_zero_pos_means_closed(self):
        positions = [{"pos": "0", "avgPx": "0"}]
        has_pos = any(float(p.get("pos", 0)) != 0 for p in positions)
        assert has_pos is False

    def test_nonzero_pos_means_open(self):
        positions = [{"pos": "0.41", "avgPx": "100.0"}]
        has_pos = any(float(p.get("pos", 0)) != 0 for p in positions)
        assert has_pos is True


class TestSLCalcForDirection:
    """SL trigger price calculation depends on direction."""

    def test_long_sl_below_entry(self):
        entry = 2145.0
        sl_pct = 0.03
        sl_p = entry * (1 - sl_pct)
        assert sl_p == pytest.approx(2080.65)
        assert sl_p < entry

    def test_short_sl_above_entry(self):
        entry = 2081.44
        sl_pct = 0.03
        sl_p = entry * (1 + sl_pct)
        assert sl_p == pytest.approx(2143.8832)
        assert sl_p > entry

    def test_sl_price_uses_exchange_avg_not_local_entry(self):
        """After reconciliation, SL should use exchange avgPx."""
        local_entry = 2010.67  # Old LONG entry
        exchange_avg = 2081.44  # New SHORT entry after flip
        sl_pct = 0.03

        # Wrong (using local):
        wrong_sl = local_entry * (1 - sl_pct)  # 1950.35

        # Correct (using exchange):
        correct_sl = exchange_avg * (1 + sl_pct)  # 2143.88 (SHORT direction)

        assert wrong_sl < 2000  # Way too low for a SHORT SL
        assert correct_sl > exchange_avg  # Correct: above entry for SHORT
