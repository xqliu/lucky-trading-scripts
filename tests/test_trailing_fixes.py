"""
Tests for trailing.py bug fixes:
1. Atomic save_state (tmp + os.replace)
2. load_state handles corrupted JSON
3. Dead code branch removed (covered by verifying check_and_update_trailing_stop return values)
"""
import json
import os
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock


@pytest.fixture
def tmp_state_file(tmp_path):
    """Provide a temporary state file path and patch STATE_FILE."""
    state_file = tmp_path / "trailing_state.json"
    with patch('luckytrader.trailing.STATE_FILE', state_file):
        yield state_file


class TestLoadStateCorruptedJson:
    """load_state() must handle corrupted JSON gracefully."""

    def test_load_corrupted_json_returns_empty(self, tmp_state_file):
        """Corrupted JSON file should return {} instead of crashing."""
        from luckytrader.trailing import load_state

        # Write corrupted JSON
        tmp_state_file.write_text("{invalid json!!!}")

        result = load_state()
        assert result == {}, f"Expected empty dict for corrupted JSON, got {result}"

    def test_load_valid_json_works(self, tmp_state_file):
        """Valid JSON should load normally."""
        from luckytrader.trailing import load_state

        state = {"BTC": {"entry_price": 67000, "trailing_active": True}}
        tmp_state_file.write_text(json.dumps(state))

        result = load_state()
        assert result == state

    def test_load_missing_file_returns_empty(self, tmp_state_file):
        """Missing state file should return {}."""
        from luckytrader.trailing import load_state

        # Don't create the file
        result = load_state()
        assert result == {}

    def test_load_empty_file_returns_empty(self, tmp_state_file):
        """Empty file should return {} instead of crashing."""
        from luckytrader.trailing import load_state

        tmp_state_file.write_text("")

        result = load_state()
        assert result == {}


class TestSaveStateAtomic:
    """save_state() must use atomic write (tmp + os.replace)."""

    def test_save_creates_file(self, tmp_state_file):
        """save_state should create the state file."""
        from luckytrader.trailing import save_state

        state = {"BTC": {"entry_price": 67000}}
        save_state(state)

        assert tmp_state_file.exists()
        loaded = json.loads(tmp_state_file.read_text())
        assert loaded == state

    def test_save_no_tmp_file_left(self, tmp_state_file):
        """After save, no .tmp file should remain."""
        from luckytrader.trailing import save_state

        save_state({"BTC": {"entry_price": 67000}})

        tmp_file = tmp_state_file.with_suffix(".tmp")
        assert not tmp_file.exists(), "Temporary file should be cleaned up after atomic rename"

    def test_save_overwrites_existing(self, tmp_state_file):
        """save_state should overwrite existing state."""
        from luckytrader.trailing import save_state

        save_state({"BTC": {"entry_price": 67000}})
        save_state({"ETH": {"entry_price": 3500}})

        loaded = json.loads(tmp_state_file.read_text())
        assert "ETH" in loaded
        assert "BTC" not in loaded


class TestCheckAndUpdateReturnValues:
    """check_and_update_trailing_stop only returns 'updated', 'error', 'no_change'.
    The dead else branch (referencing 'activation_threshold') was unreachable."""

    def test_return_values_are_valid(self):
        """All possible return values should be 'updated', 'error', or 'no_change'."""
        from luckytrader.trailing import check_and_update_trailing_stop

        position = {
            "coin": "BTC",
            "size": 0.001,
            "entry_price": 67000.0,
            "is_long": True,
        }
        state = {}

        with patch('luckytrader.trailing.get_market_price', return_value=67500.0), \
             patch('luckytrader.trailing.get_current_stop_order', return_value={
                 "oid": 123, "trigger_price": 64320.0, "order_type": "Stop", "is_trigger": True
             }):

            result = check_and_update_trailing_stop("BTC", position, state)

        assert result["action"] in ("updated", "error", "no_change"), \
            f"Unexpected action: {result['action']}"
        # 'activation_threshold' key should NOT exist in any result
        assert "activation_threshold" not in result, \
            "activation_threshold is not a valid return key"
