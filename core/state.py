"""
State persistence — atomic JSON read/write for position tracking.
"""
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def load_state(path: Path) -> Dict[str, Any]:
    """Load JSON state file. Returns empty dict if missing/corrupt."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception as e:
        logger.error(f"Failed to load state {path}: {e}")
        return {}


def save_state(path: Path, data: Dict[str, Any]) -> None:
    """Atomically save JSON state (write-to-tmp then rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=2, default=str))
    tmp.rename(path)
