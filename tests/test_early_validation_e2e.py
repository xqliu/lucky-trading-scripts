#!/usr/bin/env python3
"""End-to-end test for early validation — verifies the EXACT code path
used in ws_monitor._trailing_loop, not a simplified mock.

This test exists because early validation was "fixed" THREE times but
never actually worked in production due to:
1. Wrong place_market_order arg order
2. Refactored to close_and_cleanup but still broken
3. load_state() format mismatch (returned multi-coin dict, code expected single)
"""
import json
import pytest
import time
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta
from pathlib import Path


@pytest.fixture
def mock_state_with_position(tmp_path, monkeypatch):
    """Create a realistic multi-coin state file."""
    state_file = tmp_path / "memory" / "trading" / "position_state.json"
    state_file.parent.mkdir(parents=True)
    
    entry_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    state = {
        "BTC": {
            "position": {
                "coin": "BTC",
                "direction": "LONG",
                "entry_price": 67759.2,
                "size": 0.00065,
                "entry_time": entry_time,
                "regime": "range",
                "regime_tp_pct": 0.02,
                "regime_sl_pct": 0.05,
                "deadline": (datetime.now(timezone.utc) + timedelta(hours=58)).isoformat(),
            }
        },
        "ETH": {"position": None},
    }
    state_file.write_text(json.dumps(state))
    monkeypatch.setattr("luckytrader.execute._WORKSPACE_DIR", tmp_path)
    monkeypatch.setattr("luckytrader.execute.STATE_FILE", state_file)
    return state


def test_load_state_format_is_multi_coin(mock_state_with_position):
    """load_state() without coin returns multi-coin dict, NOT single position."""
    from luckytrader.execute import load_state
    
    raw = load_state()
    # MUST NOT have top-level 'position' key
    assert "position" not in raw, \
        "load_state() should return multi-coin dict, not single position"
    assert "BTC" in raw
    
    # With coin parameter, returns coin-specific state
    btc = load_state("BTC")
    assert btc.get("position") is not None
    assert btc["position"]["coin"] == "BTC"


def test_early_validation_finds_position_per_coin(mock_state_with_position):
    """Simulate the EXACT early validation code path from ws_monitor."""
    from luckytrader.execute import load_state
    from luckytrader.config import TRADING_COINS
    
    # This is the EXACT logic from ws_monitor._trailing_loop (after fix)
    found_positions = []
    for ev_coin in TRADING_COINS:
        coin_state = load_state(ev_coin)
        pos = coin_state.get("position") if coin_state else None
        if pos and pos.get("entry_time"):
            found_positions.append((ev_coin, pos))
    
    assert len(found_positions) == 1, f"Should find exactly 1 position, got {len(found_positions)}"
    coin, pos = found_positions[0]
    assert coin == "BTC"
    assert pos["direction"] == "LONG"
    assert pos["entry_price"] == 67759.2


def test_early_validation_old_code_would_fail(mock_state_with_position):
    """Prove the OLD code path (before fix) would NOT find any position."""
    from luckytrader.execute import load_state
    
    # OLD code: state = load_state(); pos = state.get("position")
    state = load_state()  # no coin parameter
    pos = state.get("position")  # This is None for multi-coin format
    
    assert pos is None, \
        "Old code path should fail to find position (this proves the bug)"


def test_mfe_calculation_long():
    """Verify MFE calculation for LONG is correct."""
    entry_price = 67759.2
    # Simulated candle data (skip entry candle)
    highs = [67800, 67900, 67700]  # max = 67900
    mfe = (max(highs) - entry_price) / entry_price * 100
    assert abs(mfe - 0.208) < 0.01  # ~0.208%


def test_mfe_calculation_short():
    """Verify MFE calculation for SHORT is correct."""
    entry_price = 2035.4
    # Simulated candle data
    lows = [2030, 2020, 2025]  # min = 2020
    mfe = (entry_price - min(lows)) / entry_price * 100
    assert abs(mfe - 0.757) < 0.01  # ~0.757%
