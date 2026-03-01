"""
Root conftest — global safety nets for ALL test directories.
Prevents real Discord messages and network calls from any test.
"""
from unittest.mock import patch
import pytest


@pytest.fixture(autouse=True)
def _mock_all_notifications():
    """Mock ALL notification functions across both HL and OKX systems."""
    with patch('core.notify.send_discord', return_value=True):
        yield
