from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "trader"
PROJECT_ROOT = SOURCE_ROOT.parents[1]


def test_v2_does_not_import_legacy_package() -> None:
    violations = _imports_matching(lambda name: name == "stock_analyzer" or name.startswith("stock_analyzer."))
    assert violations == []


def test_adapter_package_has_short_name() -> None:
    retired_boundary = "infra" + "structure"
    retired_package = "trader." + retired_boundary

    assert (SOURCE_ROOT / "infra").is_dir()
    assert not (SOURCE_ROOT / retired_boundary).exists()
    assert _imports_matching(lambda name: name == retired_package or name.startswith(retired_package + ".")) == []


def test_v2_dependency_direction() -> None:
    forbidden = {
        "domain": ("trader.application", "trader.infra", "trader.web", "trader.entrypoints"),
        "application": ("trader.infra", "trader.web", "trader.entrypoints"),
        "infra": ("trader.bootstrap", "trader.entrypoints", "trader.web"),
        "web": ("trader.infra",),
    }
    violations: list[str] = []
    for boundary, prefixes in forbidden.items():
        for path in (SOURCE_ROOT / boundary).rglob("*.py"):
            for imported in _imports(path):
                if imported.startswith(prefixes):
                    violations.append(f"{path.relative_to(SOURCE_ROOT)} -> {imported}")
    assert violations == []


def test_domain_has_no_io_framework_imports() -> None:
    forbidden = {"flask", "requests", "sqlite3", "subprocess", "socket"}
    violations: list[str] = []
    for path in (SOURCE_ROOT / "domain").rglob("*.py"):
        for imported in _imports(path):
            if imported.split(".", 1)[0] in forbidden:
                violations.append(f"{path.relative_to(SOURCE_ROOT)} -> {imported}")
    assert violations == []


def test_cutover_removed_legacy_runtime_tree() -> None:
    forbidden = (
        "app.py",
        "stock_analyzer",
        "static",
        "templates",
        "requirements.txt",
        "requirements",
        "config/runtime.json",
        "tests/scoring",
        "experiments",
        "analysis",
    )

    assert [path for path in forbidden if (PROJECT_ROOT / path).exists()] == []


def test_snapshot_workflow_module_uses_specific_responsibility_name() -> None:
    application = SOURCE_ROOT / "application"

    assert (application / "snapshot_workflow.py").is_file()
    assert not (application / "snapshot_lifecycle.py").exists()


def test_bootstrap_is_the_only_composition_root() -> None:
    assert not (SOURCE_ROOT / "infra" / "container.py").exists()


def test_active_product_source_files_do_not_exceed_500_lines() -> None:
    oversized: dict[str, int] = {}
    for path in SOURCE_ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in {".py", ".css", ".js", ".html"}:
            continue
        line_count = len(path.read_text(encoding="utf-8").splitlines())
        if line_count > 500:
            oversized[path.relative_to(PROJECT_ROOT).as_posix()] = line_count

    assert oversized == {}


def _imports_matching(predicate: Callable[[str], bool]) -> list[str]:
    violations: list[str] = []
    for path in SOURCE_ROOT.rglob("*.py"):
        for imported in _imports(path):
            if predicate(imported):
                violations.append(f"{path.relative_to(SOURCE_ROOT)} -> {imported}")
    return violations


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
    return imports
