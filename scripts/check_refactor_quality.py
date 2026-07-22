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
SELECTED_RULES = ("C901", "PLR0911", "PLR0912", "PLR0913", "PLR0915", "N")
EXPECTED_COUNTS = {
    "C901": 37,
    "N818": 5,
    "PLR0911": 15,
    "PLR0912": 15,
    "PLR0913": 55,
    "PLR0915": 12,
}


def main() -> int:
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

    summary = ", ".join(f"{code}={count}" for code, count in EXPECTED_COUNTS.items())
    print(f"Strict refactor debt baseline verified: {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
