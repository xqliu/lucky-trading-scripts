"""
Shared fixtures for Lucky Trading System tests.
All exchange/network calls are mocked — no real money touched.
"""
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# We need to import our scripts, but hl_trade.py loads secrets at module level.
# Patch that out BEFORE any script is imported.
# ---------------------------------------------------------------------------

# Create a fake hl_trade module so other modules can import from it
_fake_hl = types.ModuleType("hl_trade")
_fake_hl.MAIN_WALLET = "0xFAKE_WALLET"
_fake_hl.get_market_price = MagicMock(return_value=67000.0)
_fake_hl.get_account_info = MagicMock(return_value={
    "account_value": "217.76",
    "withdrawable": "100.0",
    "positions": [],
})
_fake_hl.get_open_orders = MagicMock(return_value=[])
_fake_hl.get_open_orders_detailed = MagicMock(return_value=[])
_fake_hl.place_market_order = MagicMock(return_value={"status": "ok"})
_fake_hl.place_stop_loss = MagicMock(return_value={"status": "ok"})
_fake_hl.place_take_profit = MagicMock(return_value={"status": "ok"})
_fake_hl.cancel_order = MagicMock(return_value={"status": "ok"})
_fake_hl.load_config = MagicMock(return_value={
    "MAIN_WALLET": "0xFAKE_WALLET",
    "API_WALLET": "0xFAKE_API",
    "API_PRIVATE_KEY": "0x" + "ab" * 32,
})

sys.modules["hl_trade"] = _fake_hl
sys.modules["luckytrader.trade"] = _fake_hl

# Also mock hyperliquid SDK so signal_check can import
_fake_hl_info = types.ModuleType("hyperliquid.info")
_mock_info_instance = MagicMock()
_mock_info_instance.meta.return_value = {"universe": [
    {"name": "BTC", "szDecimals": 5},
    {"name": "ETH", "szDecimals": 4},
]}
_mock_info_instance.user_state.return_value = {
    "assetPositions": [],
    "marginSummary": {"accountValue": "217.76"},
}
_fake_hl_info.Info = MagicMock(return_value=_mock_info_instance)
sys.modules["hyperliquid"] = types.ModuleType("hyperliquid")
sys.modules["hyperliquid.info"] = _fake_hl_info
_fake_hl_utils = types.ModuleType("hyperliquid.utils")
_fake_hl_utils.constants = MagicMock()
_fake_hl_utils.constants.MAINNET_API_URL = "https://fake"
sys.modules["hyperliquid.utils"] = _fake_hl_utils
sys.modules["hyperliquid.utils.constants"] = _fake_hl_utils.constants
_fake_hl_exchange = types.ModuleType("hyperliquid.exchange")
_fake_hl_exchange.Exchange = MagicMock
sys.modules["hyperliquid.exchange"] = _fake_hl_exchange
_fake_eth = types.ModuleType("eth_account")
_fake_eth.Account = MagicMock()
sys.modules["eth_account"] = _fake_eth

# Add scripts dir to path BEFORE any script imports
SCRIPTS_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

# Ensure config module can find params.toml (it uses Path(__file__).parent)
# The real config module at scripts/config/ should work since scripts/ is in path


@pytest.fixture
def mock_hl():
    """Reset all hl_trade mocks between tests."""
    _fake_hl.get_market_price.reset_mock()
    _fake_hl.get_account_info.reset_mock()
    _fake_hl.get_open_orders_detailed.reset_mock()
    _fake_hl.place_market_order.reset_mock()
    _fake_hl.place_stop_loss.reset_mock()
    _fake_hl.place_take_profit.reset_mock()
    _fake_hl.cancel_order.reset_mock()
    
    _fake_hl.get_market_price.return_value = 67000.0
    _fake_hl.get_account_info.return_value = {
        "account_value": "217.76",
        "withdrawable": "100.0",
        "positions": [],
    }
    _fake_hl.get_open_orders_detailed.side_effect = None
    _fake_hl.get_open_orders_detailed.return_value = []
    _fake_hl.place_market_order.side_effect = None
    _fake_hl.place_market_order.return_value = {"status": "ok"}
    _fake_hl.place_stop_loss.side_effect = None
    _fake_hl.place_take_profit.side_effect = None
    _fake_hl.place_stop_loss.return_value = {"status": "ok"}
    _fake_hl.place_take_profit.return_value = {"status": "ok"}
    _fake_hl.cancel_order.return_value = {"status": "ok"}
    return _fake_hl
