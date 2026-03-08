"""
Discord notification helper — shared across trading systems.

Primary path is direct Discord-compatible HTTP to Spacebar. OpenClaw CLI
remains as a fallback so the trading systems are not coupled to one route.
"""
import json
import logging
import os
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# Default channel — can be overridden per system
DEFAULT_CHANNEL_ID = "1234567890123456789"  # #日常

# Mention targets
DISCORD_MENTIONS = "<@111111111111111111> <@222222222222222222>"  # Lawrence + Lindsey

DEFAULT_API_BASE = os.environ.get(
    "OPENCLAW_DISCORD_API_BASE",
    "https://discord.com/api/v10",
)
DEFAULT_CONFIG_PATH = Path(
    os.environ.get("OPENCLAW_CONFIG_PATH", "~/.openclaw/openclaw.json")
).expanduser()
MAX_MESSAGE_LEN = 1900


def _sanitize_message(message: str) -> str:
    if len(message) <= MAX_MESSAGE_LEN:
        return message
    return message[: MAX_MESSAGE_LEN - 16] + "\n...[truncated]"


@lru_cache(maxsize=1)
def _load_discord_bot_token() -> Optional[str]:
    try:
        payload = json.loads(DEFAULT_CONFIG_PATH.read_text())
        token = payload.get("channels", {}).get("discord", {}).get("token")
        if token:
            return token
        logger.error("Discord token missing in %s", DEFAULT_CONFIG_PATH)
    except Exception as exc:
        logger.error("Failed to read Discord token from %s: %s", DEFAULT_CONFIG_PATH, exc)
    return None


def _send_via_http(message: str, channel_id: str) -> bool:
    token = _load_discord_bot_token()
    if not token:
        return False

    try:
        response = requests.post(
            f"{DEFAULT_API_BASE}/channels/{channel_id}/messages",
            headers={
                "Authorization": f"Bot {token}",
                "Content-Type": "application/json",
            },
            json={"content": message},
            timeout=10,
        )
        if response.ok:
            return True
        logger.error(
            "Discord HTTP send failed: %s %s",
            response.status_code,
            response.text[:500],
        )
    except Exception as exc:
        logger.error("Discord HTTP send exception: %s", exc)
    return False


def _send_via_openclaw(message: str, channel_id: str) -> bool:
    env = os.environ.copy()
    env.setdefault("NODE_OPTIONS", "--max-old-space-size=512")

    try:
        result = subprocess.run(
            ["openclaw", "message", "send",
             "--channel", "discord",
             "--target", channel_id,
             "--message", message],
            capture_output=True, text=True, timeout=30, env=env,
        )
        if result.returncode != 0:
            logger.error("Discord CLI send failed: %s", result.stderr)
            return False
        return True
    except Exception as exc:
        logger.error("Discord CLI send exception: %s", exc)
        return False


def send_discord(message: str, channel_id: Optional[str] = None,
                 mention: bool = False) -> bool:
    """Send message to Discord/Spacebar.

    Args:
        message: Text to send.
        channel_id: Discord channel ID. Defaults to #日常.
        mention: If True, prepend @Lawrence @Lindsey.

    Returns:
        True if sent successfully.
    """
    ch = channel_id or DEFAULT_CHANNEL_ID

    if mention:
        message = f"{DISCORD_MENTIONS}\n{message}"
    message = _sanitize_message(message)

    if _send_via_http(message, ch):
        return True
    return _send_via_openclaw(message, ch)
