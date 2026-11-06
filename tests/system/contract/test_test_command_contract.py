from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pytest

from tests import conftest as test_configuration

ROOT = Path(__file__).resolve().parents[3]
MAKEFILE = ROOT / "Makefile"
_BUSINESS_OWNERS = {
    "download": "history",
    "training": "train",
    "recommendation": "recommendation",
    "http_api": "recommendation",
    "web": "recommendation",
    "infra": "crosscut",
    "system": "crosscut",
    "tooling": "crosscut",
}
_TEST_CATEGORIES = frozenset({"unit", "component", "contract", "integration", "performance", "js"})


def test_makefile_is_the_single_test_command_source() -> None:
    makefile = MAKEFILE.read_text(encoding="utf-8")
    assert not (ROOT / "scripts" / "test.sh").exists()
    assert "scripts/test.sh" not in makefile

    expected_targets = {
        "test-full": "tests",
        "test-fast": 'tests -m "not slow"',
        "test-train": "tests/training",
        "test-history": "tests/download",
        "test-recommendation": "tests/recommendation tests/http_api tests/web",
        "test-recommendation-runtime": 'tests/recommendation tests/infra -m "slow_runtime"',
        "test-recommendation-suppliers": 'tests/recommendation tests/infra -m "slow_supplier"',
        "test-static-contracts": 'tests/system/contract -m "slow_static"',
        "test-unit": 'tests -m "unit"',
        "test-component": 'tests -m "component"',
        "test-contract": 'tests -m "contract"',
        "test-integration": 'tests -m "integration"',
    }
    result = subprocess.run(
        ["make", "-n", "PYTEST_WORKERS=4", "PYTEST_DISTRIBUTION=loadgroup", *expected_targets],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    for arguments in expected_targets.values():
        assert f"pytest -q -n 4 --dist loadgroup {arguments}" in result.stdout
    assert "--run-format-tools" not in result.stdout


@pytest.mark.parametrize(("workers", "distribution"), (("1", "load"), ("2", "loadgroup")))
def test_parallel_test_settings_can_be_selected_explicitly(workers: str, distribution: str) -> None:
    result = subprocess.run(
        ["make", "-n", "test-train", f"PYTEST_WORKERS={workers}", f"PYTEST_DISTRIBUTION={distribution}"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert f"pytest -q -n {workers} --dist {distribution} tests/training" in result.stdout


@pytest.mark.parametrize(("business", "expected_owner"), tuple(_BUSINESS_OWNERS.items()))
def test_business_owner_follows_the_business_directory(business: str, expected_owner: str) -> None:
    assert test_configuration._business_owner(f"{business}/unit/test_example.py") == expected_owner


@dataclass
class _CollectedItem:
    path: Path
    markers: set[str] = field(default_factory=set)

    @property
    def nodeid(self) -> str:
        return self.path.relative_to(ROOT).as_posix() + "::test_example"

    def add_marker(self, marker: str | pytest.MarkDecorator) -> None:
        self.markers.add(marker if isinstance(marker, str) else marker.name)

    def get_closest_marker(self, name: str) -> str | None:
        return name if name in self.markers else None


@dataclass
class _DeselectionHook:
    deselected: list[_CollectedItem] = field(default_factory=list)

    def pytest_deselected(self, items: list[pytest.Item]) -> None:
        self.deselected.extend(cast(list[_CollectedItem], items))


@dataclass
class _CollectionConfig:
    run_format_tools: bool = False
    stash: pytest.Stash = field(default_factory=pytest.Stash)
    hook: _DeselectionHook = field(default_factory=_DeselectionHook)

    def getoption(self, name: str) -> bool:
        assert name == "run_format_tools"
        return self.run_format_tools


def _collect(config: _CollectionConfig, items: list[_CollectedItem]) -> None:
    test_configuration.pytest_collection_modifyitems(cast(pytest.Config, config), cast(list[pytest.Item], items))


def test_every_test_file_has_a_directory_owned_business_and_category() -> None:
    test_root = ROOT / "tests"
    paths = tuple(sorted(test_root.rglob("test_*.py")))
    assert paths
    items = [_CollectedItem(path) for path in paths]
    _collect(_CollectionConfig(), items)

    assert len(items) == len(paths)
    for item in items:
        relative = item.path.relative_to(test_root)
        assert relative.parts[0] in _BUSINESS_OWNERS, relative
        assert relative.parts[1] in _TEST_CATEGORIES, relative
        assert _BUSINESS_OWNERS[relative.parts[0]] in item.markers, relative
        assert relative.parts[1] in item.markers, relative


@pytest.mark.parametrize(
    ("relative_path", "message"),
    (
        ("unknown/unit/test_example.py", "business owner"),
        ("recommendation/unknown/test_example.py", "test category"),
        ("unit/application/test_example.py", "test category"),
    ),
)
def test_unknown_business_or_category_is_rejected(relative_path: str, message: str) -> None:
    items = [_CollectedItem(ROOT / "tests" / relative_path)]
    with pytest.raises(pytest.UsageError, match=message):
        _collect(_CollectionConfig(), items)


@pytest.mark.parametrize(
    "marker",
    ("slow_history", "slow_migration", "slow_runtime", "slow_supplier", "slow_static", "slow_training"),
)
def test_explicit_slow_categories_are_preserved_and_excluded_from_fast_tests(marker: str) -> None:
    item = _CollectedItem(ROOT / "tests/recommendation/unit/test_example.py", {marker})
    _collect(_CollectionConfig(), [item])
    assert item.markers == {"unit", "recommendation", "slow", marker}


def test_performance_tests_are_marked_slow_without_a_path_inventory() -> None:
    item = _CollectedItem(ROOT / "tests/download/performance/company/test_example.py")
    _collect(_CollectionConfig(), [item])
    assert item.markers == {"performance", "history", "slow"}


def test_format_tools_are_deselected_by_default_without_running_them() -> None:
    regular = _CollectedItem(ROOT / "tests/tooling/unit/test_regular.py")
    formatting = _CollectedItem(ROOT / "tests/tooling/unit/test_formatting.py", {"format_tools"})
    config = _CollectionConfig()
    items = [regular, formatting]

    _collect(config, items)

    assert items == [regular]
    assert config.hook.deselected == [formatting]
    assert config.stash[test_configuration._FORMAT_TOOLS_DESELECTED] == {formatting.nodeid}


def test_explicit_format_tool_authorization_preserves_the_selected_items() -> None:
    formatting = _CollectedItem(ROOT / "tests/tooling/unit/test_formatting.py", {"format_tools"})
    config = _CollectionConfig(run_format_tools=True)
    items = [formatting]

    _collect(config, items)

    assert items == [formatting]
    assert not config.hook.deselected
    assert config.stash.get(test_configuration._FORMAT_TOOLS_DESELECTED, set()) == set()


def test_release_target_builds_then_verifies_the_wheel_outside_the_repository() -> None:
    result = subprocess.run(
        ["make", "-n", "test-release"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert ".venv/bin/python3 -m build" in result.stdout
    assert "scripts/tools.py --business engineering --module packaging --action install -- --dist-dir dist" in result.stdout
