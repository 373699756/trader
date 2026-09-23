#!/usr/bin/env python3
"""Keep refactor naming and complexity debt from changing without review."""

from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src" / "trader"
NAMING_ROOTS = (SOURCE_ROOT, PROJECT_ROOT / "scripts", PROJECT_ROOT / "tests")
SELECTED_RULES = ("C901", "PLR0911", "PLR0912", "PLR0913", "PLR0915", "N")
EXPECTED_COUNTS: dict[str, int] = {"C901": 5, "PLR0911": 1, "PLR0913": 7}
TOP_LEVEL_SCRIPT_MANIFEST = frozenset(
    {
        "audit_historical_industry_facts.py",
        "check_refactor_quality.py",
        "check_tomorrow_training_memory.py",
        "convert_baostock_history.py",
        "diagnose_runtime.py",
        "generate_long_watchlist_asset.py",
        "h1_point_in_time_capability.py",
        "migrate_runtime_data.py",
        "point_in_time_terminal_holdout.py",
        "qualify_point_in_time_data.py",
        "repack_baostock_history_archive.py",
        "verify_wheel_install.py",
    }
)


def main() -> int:
    actual_scripts = frozenset(path.name for path in (PROJECT_ROOT / "scripts").glob("*.py"))
    if actual_scripts != TOP_LEVEL_SCRIPT_MANIFEST:
        print("top-level script inventory changed; update scripts/README.md and review the owner", file=sys.stderr)
        print(f"expected: {sorted(TOP_LEVEL_SCRIPT_MANIFEST)}", file=sys.stderr)
        print(f"actual:   {sorted(actual_scripts)}", file=sys.stderr)
        return 1

    result = subprocess.run(
        (
            sys.executable,
            "-m",
            "ruff",
            "check",
            str(SOURCE_ROOT),
            "--select",
            ",".join(SELECTED_RULES),
            "--output-format",
            "json",
        ),
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode not in {0, 1}:
        sys.stderr.write(result.stderr)
        return result.returncode

    try:
        diagnostics = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        print(f"cannot parse Ruff diagnostics: {exc}", file=sys.stderr)
        return 2
    if not isinstance(diagnostics, list):
        print("Ruff diagnostics root must be a list", file=sys.stderr)
        return 2

    actual = Counter(
        diagnostic.get("code")
        for diagnostic in diagnostics
        if isinstance(diagnostic, dict) and isinstance(diagnostic.get("code"), str)
    )
    actual_counts = {code: count for code, count in sorted(actual.items()) if count}
    if actual_counts != EXPECTED_COUNTS:
        print("strict refactor debt changed; review the diff and update EXPECTED_COUNTS", file=sys.stderr)
        print(f"expected: {EXPECTED_COUNTS}", file=sys.stderr)
        print(f"actual:   {actual_counts}", file=sys.stderr)
        return 1

    naming = subprocess.run(
        (sys.executable, "-m", "ruff", "check", *(str(path) for path in NAMING_ROOTS), "--select", "N"),
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if naming.returncode != 0:
        sys.stderr.write(naming.stdout)
        sys.stderr.write(naming.stderr)
        return naming.returncode

    print("Strict refactor debt baseline and repository-wide naming rules verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
