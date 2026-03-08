"""
Discord notification helper — shared across trading systems.

Primary path is direct Discord-compatible HTTP to Spacebar. OpenClaw CLI
remains as a fallback so the trading systems are not coupled to one route.
"""
import json
import logging
import os
import subprocess
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

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
def _load_notification_defaults() -> tuple[str, str]:
    channel_id = os.environ.get("OPENCLAW_DISCORD_CHANNEL_ID", "").strip()
    mentions = os.environ.get("OPENCLAW_DISCORD_MENTIONS", "").strip()

    for env_name in ("OKX_BB_CONFIG_DIR", "LUCKYTRADER_CONFIG_DIR"):
        cfg_dir = os.environ.get(env_name)
        if not cfg_dir:
            continue
        cfg_path = Path(cfg_dir).expanduser() / "config.toml"
        if not cfg_path.exists():
            continue
        try:
            with open(cfg_path, "rb") as fh:
                raw = tomllib.load(fh)
        except Exception as exc:
            logger.error("Failed to read notification defaults from %s: %s", cfg_path, exc)
            continue

        notifications = raw.get("notifications", {})
        if not channel_id:
            channel_id = str(notifications.get("discord_channel_id", "")).strip()
        if not mentions:
            mention_1 = str(notifications.get("discord_mention_1", "")).strip()
            mention_2 = str(notifications.get("discord_mention_2", "")).strip()
            mentions = " ".join(part for part in (mention_1, mention_2) if part).strip()

        if channel_id and mentions:
            break

    return channel_id, mentions


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
    if not channel_id:
        logger.error("Discord HTTP send skipped: missing channel_id")
        return False

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
    default_channel_id, default_mentions = _load_notification_defaults()
    ch = channel_id or default_channel_id

    if mention and default_mentions:
        message = f"{default_mentions}\n{message}"
    message = _sanitize_message(message)

    if _send_via_http(message, ch):
        return True
    return _send_via_openclaw(message, ch)
