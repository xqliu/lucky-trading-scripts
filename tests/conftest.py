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


@pytest.fixture(autouse=True)
def _block_real_side_effects():
    """Global safety net: 全局禁止测试中的真实副作用。

    三层防护：
    1. notify_discord / trigger_optimization — 阻止真实 Discord 消息和系统事件
    2. requests.post/get — 阻止 signal.py 等模块直接调 Hyperliquid API
    3. socket.socket.connect — 终极安全网，任何漏网的网络调用都会立即报错

    测试中必须阻止这些调用，无论单个测试是否有局部 @patch。
    """
    import socket as _socket
    _real_connect = _socket.socket.connect

    def _blocked_connect(self, address):
        raise ConnectionError(
            f"🚨 TEST SAFETY NET: blocked real network connection to {address}. "
            f"Add @patch to mock the network call."
        )

    # Isolate ALL state files to prevent production pollution
    import tempfile
    _test_state_dir = Path(tempfile.mkdtemp())
    _test_state_dir.mkdir(exist_ok=True)

    with patch('luckytrader.execute.notify_discord') as _mock_nd, \
         patch('luckytrader.execute.trigger_optimization') as _mock_to, \
         patch('luckytrader.execute.STATE_FILE', _test_state_dir / "position_state.json"), \
         patch('luckytrader.execute.TRADE_LOG_FILE', _test_state_dir / "trade_results.json"), \
         patch('luckytrader.execute.TRADES_FILE', _test_state_dir / "TRADES.md"), \
         patch('luckytrader.trailing.STATE_FILE', _test_state_dir / "trailing_state.json"), \
         patch('time.sleep') as _mock_sleep, \
         patch.object(_socket.socket, 'connect', _blocked_connect):
        yield {"notify_discord": _mock_nd, "trigger_optimization": _mock_to,
               "time_sleep": _mock_sleep}
