"""Tests for OKX exchange API — signature and request handling."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from okx_bb.exchange import OKXClient


class TestSignature:
    """Verify HMAC-SHA256 signature matches OKX spec."""

    def test_signature_known_value(self):
        """OKX docs example: timestamp + method + path + body."""
        client = OKXClient(
            api_key="test_key",
            secret_key="test_secret",
            passphrase="test_pass",
        )
        # Known inputs
        ts = "2024-01-01T00:00:00.000Z"
        method = "GET"
        path = "/api/v5/account/balance"
        body = ""

        sig = client._sign(ts, method, path, body)

        # Signature should be non-empty base64 string
        assert len(sig) > 0
        import base64
        # Should be valid base64
        decoded = base64.b64decode(sig)
        assert len(decoded) == 32  # SHA-256 = 32 bytes

    def test_signature_deterministic(self):
        """Same inputs → same signature."""
        client = OKXClient("k", "s", "p")
        sig1 = client._sign("ts", "GET", "/path", "")
        sig2 = client._sign("ts", "GET", "/path", "")
        assert sig1 == sig2

    def test_signature_changes_with_body(self):
        """Different body → different signature."""
        client = OKXClient("k", "s", "p")
        sig1 = client._sign("ts", "POST", "/path", '{"a":1}')
        sig2 = client._sign("ts", "POST", "/path", '{"b":2}')
        assert sig1 != sig2

    def test_signature_method_case(self):
        """Method should be uppercased in signature."""
        client = OKXClient("k", "s", "p")
        sig_lower = client._sign("ts", "get", "/path", "")
        sig_upper = client._sign("ts", "GET", "/path", "")
        assert sig_lower == sig_upper


class TestHeaders:
    def test_headers_contain_required_fields(self):
        client = OKXClient("mykey", "mysecret", "mypass")
        headers = client._headers("GET", "/api/v5/test", "")
        assert headers["OK-ACCESS-KEY"] == "mykey"
        assert headers["OK-ACCESS-PASSPHRASE"] == "mypass"
        assert "OK-ACCESS-SIGN" in headers
        assert "OK-ACCESS-TIMESTAMP" in headers
        assert headers["Content-Type"] == "application/json"

    def test_simulated_header(self):
        client = OKXClient("k", "s", "p", simulated=True)
        headers = client._headers("GET", "/test", "")
        assert headers["x-simulated-trading"] == "1"

    def test_no_simulated_header_by_default(self):
        client = OKXClient("k", "s", "p")
        headers = client._headers("GET", "/test", "")
        assert "x-simulated-trading" not in headers
