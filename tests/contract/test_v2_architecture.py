from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "trader"
PROJECT_ROOT = SOURCE_ROOT.parents[1]
ACTIVE_SOURCE_MAX_LINES = 800


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


def test_domain_is_partitioned_by_business_capability_without_legacy_paths() -> None:
    domain = SOURCE_ROOT / "domain"
    capability_packages = {"market", "recommendation", "review", "outcome"}
    retired_modules = {
        "board_scoring.py",
        "board_scoring_support.py",
        "downside.py",
        "factors.py",
        "filters.py",
        "fusion.py",
        "models.py",
        "news.py",
        "outcomes.py",
        "ranking.py",
        "recommendation_models.py",
        "research.py",
        "risk.py",
        "strategies",
        "tail.py",
    }

    assert {path.name for path in domain.iterdir() if path.is_dir() and not path.name.startswith("__")} == (
        capability_packages
    )
    assert [name for name in sorted(retired_modules) if (domain / name).exists()] == []
    assert all((domain / package / "__init__.py").is_file() for package in capability_packages)


def test_domain_does_not_expose_dynamic_compatibility_aliases() -> None:
    violations: list[str] = []
    for path in (SOURCE_ROOT / "domain").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "__getattr__"
            for node in tree.body
        ):
            violations.append(path.relative_to(SOURCE_ROOT).as_posix())

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


def test_market_data_service_uses_typed_composition_without_mixins() -> None:
    market_data = SOURCE_ROOT / "infra" / "market_data"
    service_tree = ast.parse((market_data / "service.py").read_text(encoding="utf-8"))
    service_class = next(
        node for node in service_tree.body if isinstance(node, ast.ClassDef) and node.name == "MarketFeatureService"
    )
    component_classes = {
        node.name
        for path in market_data.glob("service*.py")
        for node in ast.parse(path.read_text(encoding="utf-8")).body
        if isinstance(node, ast.ClassDef)
    }

    assert service_class.bases == []
    assert not (market_data / "service_state.py").exists()
    assert not {name for name in component_classes if name.endswith("Mixin")}
    assert {
        "QuoteStore",
        "HistoryStore",
        "HistoryWarmup",
        "ResearchLoader",
        "IntradayLoader",
        "ReferenceLoader",
    } <= component_classes


def test_active_product_source_files_do_not_exceed_configured_line_limit() -> None:
    oversized: dict[str, int] = {}
    for path in SOURCE_ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in {".py", ".css", ".js", ".html"}:
            continue
        line_count = len(path.read_text(encoding="utf-8").splitlines())
        if line_count > ACTIVE_SOURCE_MAX_LINES:
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
