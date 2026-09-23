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

    business_targets = {
        "test-fast": 'pytest -q -n 4 tests -m "not slow"',
        "test-train": 'pytest -q -n 4 tests -m "train"',
        "test-history": 'pytest -q -n 4 tests -m "history"',
        "test-recommendation": 'pytest -q -n 4 tests -m "recommendation"',
        "test-recommendation-runtime": 'pytest -q -n 4 tests -m "recommendation and slow_runtime"',
        "test-recommendation-suppliers": 'pytest -q -n 4 tests -m "recommendation and slow_supplier"',
    }
    for target, command in business_targets.items():
        result = subprocess.run(
            ["make", "-n", target],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        assert command in result.stdout


def test_business_markers_are_registered_and_selectable() -> None:
    expected_files = {
        "train": "tests/unit/application/research/test_baseline_identity_audit.py",
        "history": "tests/unit/infra/research/test_history_archive_sync.py",
        "recommendation": "tests/unit/application/test_tomorrow_freezing.py",
        "crosscut": "tests/contract/test_test_command_contract.py",
    }
    for marker, test_path in expected_files.items():
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "--collect-only",
                "-q",
                "-m",
                marker,
                test_path,
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        assert test_path in result.stdout


def test_slow_recommendation_subsets_are_selectable() -> None:
    expected_files = {
        "recommendation and slow_runtime": "tests/unit/application/test_input_runtime.py",
        "recommendation and slow_supplier": "tests/component/test_market_gateway.py",
    }
    for marker, test_path in expected_files.items():
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "--collect-only",
                "-q",
                "-m",
                marker,
                test_path,
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        assert test_path in result.stdout


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
