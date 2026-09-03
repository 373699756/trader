from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = ROOT / "Makefile"


def test_makefile_is_the_single_test_command_source() -> None:
    makefile = MAKEFILE.read_text(encoding="utf-8")

    assert not (ROOT / "scripts" / "test.sh").exists()
    assert "scripts/test.sh" not in makefile

    expected_targets = {
        "test": "tests",
        "test-unit": "tests/unit",
        "test-component": "tests/component",
        "test-contract": "tests/contract",
        "test-integration": "tests/integration",
    }
    for target, test_path in expected_targets.items():
        result = subprocess.run(
            ["make", "-n", target],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        assert f"pytest -q {test_path}" in result.stdout
