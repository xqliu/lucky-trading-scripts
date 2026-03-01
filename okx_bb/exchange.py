"""
OKX Exchange API Wrapper
=========================
REST API for order management, account info, market data.
All OKX-specific protocol details are encapsulated here.

Docs: https://www.okx.com/docs-v5/en/
"""
import hashlib
import hmac
import base64
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://www.okx.com"
API_PREFIX = "/api/v5"

# Rate limit: 5s base delay on 429 (same principle as HL)
RETRY_BASE_DELAY = 5
MAX_RETRIES = 3


class OKXClient:
    """OKX REST API client for futures trading."""

    def __init__(self, api_key: str, secret_key: str, passphrase: str,
                 simulated: bool = False):
        """
        Args:
            api_key: OKX API key
            secret_key: OKX secret key
            passphrase: OKX passphrase
            simulated: If True, use simulated trading (demo mode)
        """
        self.api_key = api_key
        self.secret_key = secret_key
        self.passphrase = passphrase
        self.simulated = simulated
        self.session = requests.Session()

    def _sign(self, timestamp: str, method: str, path: str,
              body: str = "") -> str:
        """Generate HMAC-SHA256 signature for OKX API."""
        message = timestamp + method.upper() + path + body
        mac = hmac.new(
            self.secret_key.encode('utf-8'),
            message.encode('utf-8'),
            hashlib.sha256
        )
        return base64.b64encode(mac.digest()).decode('utf-8')

    def _headers(self, method: str, path: str, body: str = "") -> dict:
        """Build authenticated request headers."""
        ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
        sig = self._sign(ts, method, path, body)
        headers = {
            'OK-ACCESS-KEY': self.api_key,
            'OK-ACCESS-SIGN': sig,
            'OK-ACCESS-TIMESTAMP': ts,
            'OK-ACCESS-PASSPHRASE': self.passphrase,
            'Content-Type': 'application/json',
        }
        if self.simulated:
            headers['x-simulated-trading'] = '1'
        return headers

    def _request(self, method: str, endpoint: str,
                 params: Optional[dict] = None,
                 body: Optional[Any] = None) -> dict:
        """Make authenticated API request with retry on 429."""
        path = API_PREFIX + endpoint
        url = BASE_URL + path

        body_str = json.dumps(body) if body else ""
        if params:
            path += "?" + urlencode(params)
            url += "?" + urlencode(params)

        for attempt in range(MAX_RETRIES + 1):
            try:
                headers = self._headers(method, path, body_str)
                if method == "GET":
                    resp = self.session.get(url, headers=headers, timeout=15)
                else:
                    resp = self.session.post(url, headers=headers,
                                             data=body_str, timeout=15)

                if resp.status_code == 429:
                    delay = RETRY_BASE_DELAY * (2 ** attempt)
                    logger.warning(f"429 rate limited, retry in {delay}s "
                                   f"(attempt {attempt + 1})")
                    time.sleep(delay)
                    continue

                try:
                    data = resp.json()
                except (json.JSONDecodeError, ValueError) as e:
                    logger.error(f"Invalid JSON response (status={resp.status_code}): {e}")
                    if attempt < MAX_RETRIES:
                        time.sleep(RETRY_BASE_DELAY)
                        continue
                    return {"code": "-1", "msg": f"Invalid JSON: {e}"}

                # OKX returns code "0" for success
                if data.get("code") != "0":
                    logger.error(f"OKX API error: {data}")
                return data

            except requests.exceptions.Timeout:
                logger.warning(f"Request timeout (attempt {attempt + 1})")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_BASE_DELAY)
            except Exception as e:
                logger.error(f"Request exception: {e}")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_BASE_DELAY)

        return {"code": "-1", "msg": "Max retries exceeded"}

    # === Account ===

    def get_balance(self) -> Dict[str, Any]:
        """Get account balance (trading account)."""
        data = self._request("GET", "/account/balance")
        if data.get("code") == "0" and data.get("data"):
            details = data["data"][0].get("details", [])
            total_eq = float(data["data"][0].get("totalEq", 0))
            usdt_avail = 0.0
            for d in details:
                if d.get("ccy") == "USDT":
                    usdt_avail = float(d.get("availBal", 0))
            return {
                "total_equity": total_eq,
                "usdt_available": usdt_avail,
                "raw": data["data"][0],
            }
        return {"total_equity": 0, "usdt_available": 0, "error": data.get("msg")}

    def get_positions(self, instId: Optional[str] = None) -> Optional[List[dict]]:
        """Get open positions.

        Returns:
            List of positions on success (may be empty).
            None on API error (distinguishes from "no positions").
        """
        params = {}
        if instId:
            params["instId"] = instId
        data = self._request("GET", "/account/positions", params=params)
        if data.get("code") == "0":
            return data.get("data", [])
        logger.error(f"get_positions API error: {data}")
        return None  # API error, not "no positions"

    # === Market Data ===

    def get_ticker(self, instId: str) -> Optional[Dict[str, float]]:
        """Get latest ticker price."""
        data = self._request("GET", "/market/ticker", {"instId": instId})
        if data.get("code") == "0" and data.get("data"):
            t = data["data"][0]
            return {
                "last": float(t["last"]),
                "bid": float(t["bidPx"]),
                "ask": float(t["askPx"]),
                "vol24h": float(t.get("vol24h", 0)),
            }
        return None

    def get_candles(self, instId: str, bar: str = "30m",
                    limit: int = 300) -> List[dict]:
        """Get candlestick data.

        Args:
            instId: Instrument ID (e.g. "ETH-USDT-SWAP")
            bar: Candle interval (e.g. "30m", "1H", "4H")
            limit: Max candles (up to 300)

        Returns:
            List of {ts, o, h, l, c, vol} dicts, oldest first.
        """
        data = self._request("GET", "/market/candles",
                             {"instId": instId, "bar": bar, "limit": str(limit)})
        if data.get("code") == "0" and data.get("data"):
            candles = []
            for row in reversed(data["data"]):  # OKX returns newest first
                candles.append({
                    "ts": int(row[0]),
                    "o": float(row[1]),
                    "h": float(row[2]),
                    "l": float(row[3]),
                    "c": float(row[4]),
                    "vol": float(row[5]),
                })
            return candles
        return []

    # === Trading ===

    def place_market_order(self, instId: str, side: str,
                           sz: str, reduceOnly: bool = False) -> dict:
        """Place market order.

        Args:
            instId: e.g. "ETH-USDT-SWAP"
            side: "buy" or "sell"
            sz: Size in contracts
            reduceOnly: Close-only order
        """
        body = {
            "instId": instId,
            "tdMode": "isolated",  # isolated margin
            "side": side,
            "ordType": "market",
            "sz": sz,
        }
        if reduceOnly:
            body["reduceOnly"] = "true"
        return self._request("POST", "/trade/order", body=body)

    def place_limit_order(self, instId: str, side: str,
                          sz: str, px: str,
                          reduceOnly: bool = False) -> dict:
        """Place limit order (for TP)."""
        body = {
            "instId": instId,
            "tdMode": "isolated",
            "side": side,
            "ordType": "limit",
            "sz": sz,
            "px": px,
        }
        if reduceOnly:
            body["reduceOnly"] = "true"
        return self._request("POST", "/trade/order", body=body)

    def place_stop_order(self, instId: str, side: str, sz: str,
                         slTriggerPx: str, slOrdPx: str = "-1") -> dict:
        """Place stop-loss order (algo order).

        Args:
            slOrdPx: "-1" means market price on trigger
        """
        body = {
            "instId": instId,
            "tdMode": "isolated",
            "side": side,
            "ordType": "conditional",
            "sz": sz,
            "slTriggerPx": slTriggerPx,
            "slOrdPx": slOrdPx,
            "slTriggerPxType": "last",
        }
        return self._request("POST", "/trade/order-algo", body=body)

    def place_trigger_order(self, instId: str, side: str, sz: str,
                            triggerPx: str, orderPx: str = "-1",
                            triggerPxType: str = "last") -> dict:
        """Place a trigger (conditional) order — fires market order when price hits trigger.

        Args:
            triggerPx: Price that triggers the order
            orderPx: "-1" for market order on trigger
            triggerPxType: "last", "index", or "mark"
        """
        body = {
            "instId": instId,
            "tdMode": "isolated",
            "side": side,
            "ordType": "trigger",
            "sz": sz,
            "triggerPx": triggerPx,
            "orderPx": orderPx,
            "triggerPxType": triggerPxType,
        }
        return self._request("POST", "/trade/order-algo", body=body)

    def cancel_order(self, instId: str, ordId: str) -> dict:
        """Cancel a regular order."""
        return self._request("POST", "/trade/cancel-order",
                             body={"instId": instId, "ordId": ordId})

    def cancel_algo_order(self, algoId: str, instId: str) -> dict:
        """Cancel an algo order (stop-loss/take-profit)."""
        return self._request("POST", "/trade/cancel-algos",
                             body=[{"algoId": algoId, "instId": instId}])

    def get_open_orders(self, instId: Optional[str] = None) -> List[dict]:
        """Get open regular orders."""
        params = {}
        if instId:
            params["instId"] = instId
        data = self._request("GET", "/trade/orders-pending", params=params)
        if data.get("code") == "0":
            return data.get("data", [])
        return []

    def get_algo_orders(self, instId: Optional[str] = None,
                        ordType: str = "conditional") -> List[dict]:
        """Get pending algo orders (SL/TP)."""
        params = {"ordType": ordType}
        if instId:
            params["instId"] = instId
        data = self._request("GET", "/trade/orders-algo-pending", params=params)
        if data.get("code") == "0":
            return data.get("data", [])
        return []

    def set_leverage(self, instId: str, lever: str,
                     mgnMode: str = "isolated") -> dict:
        """Set leverage for instrument."""
        return self._request("POST", "/account/set-leverage",
                             body={"instId": instId, "lever": lever,
                                   "mgnMode": mgnMode})

    def get_order_detail(self, instId: str, ordId: str) -> Optional[dict]:
        """Get order detail including fill price."""
        data = self._request("GET", "/trade/order",
                             {"instId": instId, "ordId": ordId})
        if data.get("code") == "0" and data.get("data"):
            return data["data"][0]
        return None

    def get_fills(self, instId: Optional[str] = None,
                  limit: int = 10) -> List[dict]:
        """Get recent fills (last 3 days)."""
        params = {"limit": str(limit)}
        if instId:
            params["instId"] = instId
        data = self._request("GET", "/trade/fills", params=params)
        if data.get("code") == "0":
            return data.get("data", [])
        return []

    def get_algo_order_history(self, ordType: str = "conditional",
                                instId: Optional[str] = None,
                                limit: int = 10,
                                state: Optional[str] = None) -> List[dict]:
        """Get algo order history (filled/cancelled).

        Use to determine if SL or TP was triggered.
        State: 'effective' (triggered), 'canceled', 'order_failed'
        """
        params = {"ordType": ordType, "limit": str(limit)}
        if instId:
            params["instId"] = instId
        if state:
            params["state"] = state
        data = self._request("GET", "/trade/orders-algo-history",
                             params=params)
        if data.get("code") == "0":
            return data.get("data", [])
        return []

    # === Utility ===

    def get_instrument(self, instId: str) -> Optional[dict]:
        """Get instrument info (lot size, tick size, etc.)."""
        data = self._request("GET", "/public/instruments",
                             {"instType": "SWAP", "instId": instId})
        if data.get("code") == "0" and data.get("data"):
            return data["data"][0]
        return None
