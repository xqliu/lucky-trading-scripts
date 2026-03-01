"""Enforce: no indicator implementations outside core/indicators.py."""
import sys
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

FORBIDDEN_DEFS = [
    r'def\s+ema\s*\(',
    r'def\s+rsi\s*\(',
    r'def\s+bollinger_bands\s*\(',
]


class TestNoIndicatorDuplication:
    def test_no_indicators_in_okx_bb(self):
        """okx_bb/ should not define ema/rsi/bollinger_bands."""
        okx_dir = Path(__file__).parent.parent
        violations = []
        for py_file in okx_dir.rglob("*.py"):
            if "core" in py_file.parts or "test" in py_file.name:
                continue
            content = py_file.read_text()
            for pattern in FORBIDDEN_DEFS:
                if re.search(pattern, content):
                    violations.append(f"{py_file.name}: {pattern}")
        assert violations == [], f"Indicator duplication found: {violations}"

    def test_strategy_imports_from_core(self):
        """strategy.py must import from core.indicators."""
        strategy_file = Path(__file__).parent.parent / "strategy.py"
        content = strategy_file.read_text()
        assert "from core.indicators import" in content
