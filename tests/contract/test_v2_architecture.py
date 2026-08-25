from __future__ import annotations

import ast
from dataclasses import MISSING, fields
from pathlib import Path

from trader.application.ports.v2_runtime import V2DecisionBuilderPort
from trader.application.v2_runtime import V2RuntimeDependencies

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "trader"
PROJECT_ROOT = SOURCE_ROOT.parents[1]


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
    return result


def test_v2_does_not_import_legacy_package() -> None:
    violations = []
    for path in SOURCE_ROOT.rglob("*.py"):
        violations.extend(
            f"{path}:{name}"
            for name in _imports(path)
            if name == "stock_analyzer" or name.startswith("stock_analyzer.")
        )
    assert violations == []


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


def test_old_production_chain_has_no_active_files() -> None:
    retired = (
        "application/candidate_features.py",
        "application/pipeline.py",
        "application/published_snapshots.py",
        "application/publisher.py",
        "application/queries.py",
        "application/recommendation_replay.py",
        "application/ports/decision_freezes.py",
        "application/today_v2_runtime.py",
        "application/tomorrow_v2_runtime.py",
        "application/tomorrow_shadow.py",
        "application/tomorrow_shadow_runtime.py",
        "application/tomorrow_shadow_projection.py",
        "application/tomorrow_shadow_types.py",
        "application/tomorrow_research_trace.py",
        "application/trading_session.py",
        "domain/recommendation/tomorrow_freeze.py",
        "infra/market_data/provider_adapter.py",
        "infra/persistence/snapshots.py",
        "infra/persistence/snapshot_files.py",
        "infra/persistence/snapshot_replay.py",
        "infra/persistence/migration.py",
        "web/routes_events.py",
        "web/routes_recommendations.py",
        "web/routes_status.py",
        "web/sse.py",
        "web/templates/tomorrow_v2.html",
    )
    assert [name for name in retired if (SOURCE_ROOT / name).exists()] == []


def test_active_web_surface_and_fixed_long_tabs_are_v2_only() -> None:
    web = SOURCE_ROOT / "web"
    assert {"routes.py", "routes_v2.py", "decision_serializers.py", "decision_sse.py"} <= {
        path.name for path in web.glob("*.py")
    }
    dashboard = (web / "static/dashboard.js").read_text(encoding="utf-8")
    groups = (web / "static/long_groups.js").read_text(encoding="utf-8")
    assert "/api/v2/decisions/" in dashboard
    assert "/api/recommendations/" not in dashboard
    assert '"chokepoint"' in groups
    assert '"future_growth"' in groups
    assert '"low_price_potential"' in groups


def test_domain_has_no_io_framework_imports() -> None:
    forbidden = {"flask", "requests", "sqlite3", "subprocess", "socket"}
    violations = []
    for path in (SOURCE_ROOT / "domain").rglob("*.py"):
        violations.extend(f"{path}:{name}" for name in _imports(path) if name.split(".", 1)[0] in forbidden)
    assert violations == []


def test_bootstrap_is_the_only_composition_root() -> None:
    assert not (SOURCE_ROOT / "infra/container.py").exists()


def test_application_runtime_modules_are_reachable_from_bootstrap() -> None:
    runtime_modules = {f"trader.application.{path.stem}" for path in (SOURCE_ROOT / "application").glob("*runtime.py")}
    assert runtime_modules <= _imports(SOURCE_ROOT / "bootstrap.py")


def test_bootstrap_wires_overlay_events_into_the_unified_scheduler() -> None:
    source = (SOURCE_ROOT / "bootstrap.py").read_text(encoding="utf-8")
    assert "publish_overlay=publication.decision_events.publish_overlay" in source


def test_overlay_publisher_and_input_quality_are_required_typed_runtime_boundaries() -> None:
    publisher = next(field for field in fields(V2RuntimeDependencies) if field.name == "publish_overlay")

    assert publisher.default is MISSING
    assert "input_quality_status" in V2DecisionBuilderPort.__dict__


def test_internal_state_is_typed_until_an_explicit_observability_boundary() -> None:
    violations: list[str] = []
    conversion_names = {"as_dict", "to_json", "to_status"}
    exempt_status_paths = {
        Path("application/ports/reviews.py"),
        Path("infra/deepseek/reviewer.py"),
        Path("infra/deepseek/reviewer_status.py"),
    }
    trader_root = SOURCE_ROOT
    forbidden_status_types = {"Any", "JsonObject", "JsonValue", "Mapping", "MutableMapping", "dict", "object"}
    for path in trader_root.rglob("*.py"):
        relative = path.relative_to(trader_root)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for class_node in (node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)):
            for member in class_node.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)) and member.name in conversion_names:
                    violations.append(f"{relative}:{member.lineno}: state object owns {member.name} serialization")
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name != "status" or relative in exempt_status_paths:
                continue
            annotation = node.returns
            if annotation is None:
                violations.append(f"{relative}:{node.lineno}: status return is not annotated")
                continue
            annotation_names = {
                child.id if isinstance(child, ast.Name) else child.attr
                for child in ast.walk(annotation)
                if isinstance(child, (ast.Name, ast.Attribute))
            }
            if forbidden_status_types & annotation_names:
                rendered = ast.unparse(annotation)
                violations.append(f"{relative}:{node.lineno}: untyped status return {rendered}")
    assert violations == []
