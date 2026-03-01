"""Shared fixtures for OKX BB tests."""
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _block_okx_side_effects():
    """Mock time.sleep, send_discord and network calls for OKX BB tests.

    Key: patch send_discord where it's imported (okx_bb.executor),
    not just where it's defined (core.notify).
    """
    import socket as _socket

    def _blocked_connect(self, address):
        raise ConnectionError(
            f"🚨 TEST SAFETY NET: blocked real network connection to {address}"
        )

    with patch('time.sleep'), \
         patch('okx_bb.executor.time.sleep'), \
         patch('okx_bb.exchange.time.sleep'), \
         patch('okx_bb.executor.send_discord', return_value=True), \
         patch('core.notify.send_discord', return_value=True), \
         patch.object(_socket.socket, 'connect', _blocked_connect):
        yield
