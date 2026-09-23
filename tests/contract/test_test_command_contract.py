from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from conftest import _SLOW_MARKERS_BY_PATH, _business_owner, pytest_collection_modifyitems

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
        "test-static-contracts": 'pytest -q -n 4 tests -m "crosscut and slow_static"',
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


@pytest.mark.parametrize(
    ("relative_path", "expected_owner"),
    (
        ("unit/application/research/test_baseline_identity_audit.py", "train"),
        ("unit/infra/research/test_history_archive_sync.py", "history"),
        ("unit/application/test_tomorrow_freezing.py", "recommendation"),
        ("contract/test_test_command_contract.py", "crosscut"),
    ),
)
def test_business_owner_mapping_is_explicit(relative_path: str, expected_owner: str) -> None:
    assert _business_owner(relative_path) == expected_owner


def test_slow_recommendation_subsets_are_selectable() -> None:
    runtime_path = "unit/application/test_input_runtime.py"
    supplier_path = "component/test_market_gateway.py"

    assert _business_owner(runtime_path) == "recommendation"
    assert runtime_path in _SLOW_MARKERS_BY_PATH["slow_runtime"]
    assert _business_owner(supplier_path) == "recommendation"
    assert supplier_path in _SLOW_MARKERS_BY_PATH["slow_supplier"]
    assert "contract/test_professional_naming_contract.py" in _SLOW_MARKERS_BY_PATH["slow_static"]


@dataclass
class _CollectedItem:
    path: Path
    markers: set[str] = field(default_factory=set)

    def add_marker(self, marker: str | pytest.MarkDecorator) -> None:
        self.markers.add(marker if isinstance(marker, str) else marker.name)


def test_collection_hook_adds_directory_owner_and_slow_markers() -> None:
    unit = _CollectedItem(ROOT / "tests/unit/test_server_entrypoint.py")
    slow_runtime = _CollectedItem(ROOT / "tests/unit/application/test_input_runtime.py")

    pytest_collection_modifyitems([unit, slow_runtime])  # type: ignore[list-item]

    assert unit.markers == {"unit", "crosscut"}
    assert slow_runtime.markers == {"unit", "recommendation", "slow", "slow_runtime"}


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
