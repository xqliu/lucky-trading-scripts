#!/usr/bin/env python3
"""
Enforce branch coverage gates on core trading paths.

Core modules (must be ≥ 90% branch coverage):
  - okx_bb/executor.py        (order placement / position close)
  - okx_bb/ws_monitor.py      (real-time entry / SL / TP)
  - okx_bb/strategy.py        (signal generation)
  - okx_sol_bb/executor.py
  - okx_sol_bb/ws_monitor.py
  - okx_sol_bb/strategy.py
  - luckytrader/execute.py     (HL order execution)
  - luckytrader/ws_monitor.py  (HL real-time monitor)
  - luckytrader/signal.py      (HL signal generation)
  - luckytrader/trailing.py    (trailing stop logic)
  - core/indicators.py         (shared indicators)

Non-core modules: ≥ 60% (warn if below, don't fail).
"""

import re
import sys

CORE_MODULES = {
    "okx_bb/executor.py",
    "okx_bb/ws_monitor.py",
    "okx_bb/strategy.py",
    "okx_sol_bb/executor.py",
    "okx_sol_bb/ws_monitor.py",
    "okx_sol_bb/strategy.py",
    "luckytrader/execute.py",
    "luckytrader/ws_monitor.py",
    "luckytrader/signal.py",
    "luckytrader/trailing.py",
    "core/indicators.py",
}

CORE_THRESHOLD = 90
WARN_THRESHOLD = 60


def parse_coverage(path: str) -> dict[str, int]:
    """Parse pytest-cov terminal output into {module: coverage%}."""
    results = {}
    with open(path) as f:
        for line in f:
            # Match lines like: luckytrader/execute.py   755   183   300   50    76%
            # Branch mode has 5 numeric columns: Stmts Miss Branch BrPart Cover%
            m = re.match(r"^(\S+\.py)\s+\d+\s+\d+\s+\d+\s+\d+\s+(\d+)%", line)
            if m:
                module = m.group(1)
                pct = int(m.group(2))
                results[module] = pct
    return results


def main():
    if len(sys.argv) < 2:
        print("Usage: check_coverage.py <coverage_output.txt>")
        sys.exit(1)

    results = parse_coverage(sys.argv[1])

    if not results:
        print("❌ No coverage data found. Did pytest-cov run with --cov-branch?")
        sys.exit(1)

    print(f"📊 Parsed {len(results)} modules\n")

    failures = []
    warnings = []

    for module in sorted(CORE_MODULES):
        pct = results.get(module)
        if pct is None:
            failures.append(f"  ❌ {module}: NOT FOUND in coverage output")
        elif pct < CORE_THRESHOLD:
            failures.append(f"  ❌ {module}: {pct}% < {CORE_THRESHOLD}% required")
        else:
            print(f"  ✅ {module}: {pct}%")

    print()
    for module, pct in sorted(results.items()):
        if module not in CORE_MODULES and pct < WARN_THRESHOLD:
            warnings.append(f"  ⚠️  {module}: {pct}% < {WARN_THRESHOLD}%")

    if warnings:
        print("Non-core modules below warning threshold:")
        for w in warnings:
            print(w)
        print()

    if failures:
        print("🚨 CORE PATH COVERAGE GATE FAILED:")
        for f in failures:
            print(f)
        print(f"\nAll core modules must have ≥ {CORE_THRESHOLD}% branch coverage.")
        sys.exit(1)

    print("✅ All core modules pass branch coverage gate.")


if __name__ == "__main__":
    main()
