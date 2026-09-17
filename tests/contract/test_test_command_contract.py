from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = ROOT / "Makefile"


def test_makefile_is_the_single_test_command_source() -> None:
    makefile = MAKEFILE.read_text(encoding="utf-8")

    assert not (ROOT / "scripts" / "test.sh").exists()
    assert "scripts/test.sh" not in makefile

    expected_targets = {
        "test-full": "tests",
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
        assert f"pytest -q -n 4 {test_path}" in result.stdout

    default_result = subprocess.run(
        ["make", "-n", "test"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert 'pytest -q -n 4 tests -m "not slow"' in default_result.stdout


def test_pytest_directory_markers_are_registered_and_selectable() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-m",
            "unit",
            "tests/unit/test_server_entrypoint.py",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "tests/unit/test_server_entrypoint.py: 3" in result.stdout


def test_release_target_builds_then_verifies_the_wheel_outside_the_repository() -> None:
    result = subprocess.run(
        ["make", "-n", "test-release"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert ".venv/bin/python3 -m build" in result.stdout
    assert "scripts/verify_wheel_install.py --dist-dir dist" in result.stdout
