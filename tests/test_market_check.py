"""
Tests for market_check.py — cron job that runs every 30 minutes.
"""
import pytest
from unittest.mock import patch, MagicMock


class TestGetPrices:
    """Price fetching with validation."""
    
    @patch('requests.post')
    def test_normal_prices(self, mock_post):
        from luckytrader.monitor import get_prices
        mock_post.return_value = MagicMock(
            json=MagicMock(return_value={"BTC": "67000.50", "ETH": "1975.30"})
        )
        result = get_prices()
        assert result["BTC"] == 67000.50
        assert result["ETH"] == 1975.30
    
    @patch('requests.post')
    def test_rejects_invalid_prices(self, mock_post):
        """Prices below sanity threshold should be rejected."""
        from luckytrader.monitor import get_prices
        mock_post.return_value = MagicMock(
            json=MagicMock(return_value={"BTC": "0", "ETH": "0"})
        )
        result = get_prices()
        assert result is None
    
    @patch('requests.post')
    def test_network_error(self, mock_post):
        from luckytrader.monitor import get_prices
        mock_post.side_effect = Exception("Connection timeout")
        result = get_prices()
        assert result is None


class TestCheckAlerts:
    """Price alert triggering."""
    
    def test_no_alerts(self):
        from luckytrader.monitor import check_alerts
        prices = {"BTC": 67000, "ETH": 1975}
        alerts = check_alerts(prices)
        assert alerts == []
    
    def test_btc_below_support(self):
        from luckytrader.monitor import check_alerts
        prices = {"BTC": 64000, "ETH": 1975}
        alerts = check_alerts(prices)
        assert any("BTC" in a and "65K" in a for a in alerts)
    
    def test_btc_above_resistance(self):
        from luckytrader.monitor import check_alerts
        prices = {"BTC": 71000, "ETH": 1975}
        alerts = check_alerts(prices)
        assert any("BTC" in a and "70K" in a for a in alerts)
    
    def test_multiple_alerts(self):
        from luckytrader.monitor import check_alerts
        prices = {"BTC": 64000, "ETH": 1800}
        alerts = check_alerts(prices)
        assert len(alerts) >= 2  # both BTC and ETH triggered
