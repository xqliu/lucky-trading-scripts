"""Shared fixtures for OKX SOL BB tests."""
from unittest.mock import patch
from pathlib import Path

import pytest
import socket as _socket


@pytest.fixture(autouse=True)
def _block_sol_side_effects(tmp_path):
    """Mock send_discord at all import points, block network, isolate state."""
    test_state_dir = tmp_path / "state"
    test_state_dir.mkdir()

    def _blocked_connect(self, address):
        raise ConnectionError(
            f"🚨 TEST SAFETY NET: blocked real network connection to {address}"
        )

    with patch('time.sleep'), \
         patch('core.notify.send_discord', return_value=True), \
         patch('okx_sol_bb.executor.send_discord', return_value=True), \
         patch('okx_sol_bb.ws_monitor._send_discord_sync', return_value=True), \
         patch('okx_sol_bb.executor.STATE_DIR', test_state_dir), \
         patch('okx_sol_bb.executor.POSITION_STATE_FILE', test_state_dir / "position_state.json"), \
         patch('okx_sol_bb.executor.TRADE_LOG_FILE', test_state_dir / "trade_log.json"), \
         patch.object(_socket.socket, 'connect', _blocked_connect):
        yield
