"""
Discord notification helper — shared across trading systems.
Uses OpenClaw's message tool via subprocess.
"""
import json
import logging
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)

# Default channel — can be overridden per system
DEFAULT_CHANNEL_ID = "1469405365849313831"  # #日常

# Mention targets
DISCORD_MENTIONS = "<@1469390967256703013> <@1469405440289821357>"  # Lawrence + Lindsey


def send_discord(message: str, channel_id: Optional[str] = None,
                 mention: bool = False) -> bool:
    """Send message to Discord via OpenClaw CLI.

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

    try:
        result = subprocess.run(
            ["openclaw", "message", "send",
             "--channel", "discord",
             "--target", ch,
             "--message", message],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            logger.error(f"Discord send failed: {result.stderr}")
            return False
        return True
    except Exception as e:
        logger.error(f"Discord send exception: {e}")
        return False
