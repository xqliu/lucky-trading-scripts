"""Shared fixtures for OKX BB tests."""
from unittest.mock import patch
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _block_okx_side_effects(tmp_path):
    """Mock time.sleep, send_discord, network calls, and isolate state dir.

    Key: patch send_discord where it's imported (okx_bb.executor),
    not just where it's defined (core.notify).
    State files go to tmp_path to avoid polluting production state.
    """
    import socket as _socket

    def _blocked_connect(self, address):
        raise ConnectionError(
            f"🚨 TEST SAFETY NET: blocked real network connection to {address}"
        )

    # Isolate state directory so tests don't write to production state
    test_state_dir = tmp_path / "state"
    test_state_dir.mkdir()

    with patch('time.sleep'), \
         patch('okx_bb.executor.time.sleep'), \
         patch('okx_bb.exchange.time.sleep'), \
         patch('okx_bb.executor.send_discord', return_value=True), \
         patch('core.notify.send_discord', return_value=True), \
         patch('okx_bb.executor.STATE_DIR', test_state_dir), \
         patch('okx_bb.executor.POSITION_STATE_FILE', test_state_dir / "position_state.json"), \
         patch('okx_bb.executor.TRADE_LOG_FILE', test_state_dir / "trade_log.json"), \
         patch.object(_socket.socket, 'connect', _blocked_connect):
        yield
