import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from okx_bb.config import load_config


def test_execution_defaults_present():
    cfg = load_config()
    assert cfg.execution.mode in {"close_confirm_buffer", "intrabar_trigger", "close"}
    assert cfg.execution.entry_buffer_pct >= 0
