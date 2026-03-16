from types import SimpleNamespace
from unittest.mock import patch

from core import notify


def test_send_discord_prefers_http_and_skips_cli():
    notify._load_discord_bot_token.cache_clear()
    notify._load_notification_defaults.cache_clear()

    with patch("core.notify._load_discord_bot_token", return_value="token123"), \
         patch("core.notify.requests.post", return_value=SimpleNamespace(ok=True)), \
         patch("core.notify.subprocess.run") as mock_run:
        ok = notify.send_discord("hello", channel_id="123", mention=True)

    assert ok is True
    mock_run.assert_not_called()


def test_send_discord_falls_back_to_cli_when_http_fails():
    notify._load_discord_bot_token.cache_clear()
    notify._load_notification_defaults.cache_clear()

    with patch("core.notify._load_discord_bot_token", return_value="token123"), \
         patch("core.notify.requests.post", side_effect=RuntimeError("boom")) as mock_post, \
         patch("core.notify.subprocess.run", return_value=SimpleNamespace(returncode=0, stderr="")) as mock_run:
        http_ok = notify._send_via_http("hello", "123")
        assert http_ok is False, f"HTTP should fail but returned {http_ok}"
        assert mock_post.called, "requests.post should have been called"

        ok = notify._send_via_openclaw("hello", "123")
        assert ok is True
        assert mock_run.called, "subprocess.run should have been called for CLI fallback"


def test_send_discord_truncates_long_messages():
    long_message = "x" * 5000
    clean = notify._sanitize_message(long_message)
    assert len(clean) <= notify.MAX_MESSAGE_LEN
    assert clean.endswith("...[truncated]")
