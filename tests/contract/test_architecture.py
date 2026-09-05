from __future__ import annotations

import ast
import subprocess
from dataclasses import MISSING, fields
from pathlib import Path

from trader.application.ports.scheduler import DecisionBuilderPort
from trader.application.runtime.scheduler_runtime import RuntimeDependencies

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "trader"
PROJECT_ROOT = SOURCE_ROOT.parents[1]


def test_tracked_repository_uses_precise_resource_and_state_names() -> None:
    prohibited = "life" + "cycle"
    tracked = (
        subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=PROJECT_ROOT,
            check=True,
            capture_output=True,
        )
        .stdout.decode("utf-8")
        .split("\0")
    )
    tracked_paths = [path for path in tracked if path]
    path_violations = [path for path in tracked_paths if prohibited in path.casefold()]
    content_violations: list[str] = []
    for relative in tracked_paths:
        path = PROJECT_ROOT / relative
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if prohibited in content.casefold():
            content_violations.append(relative)

    assert path_violations == []
    assert content_violations == []


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
    return result


def test_active_code_does_not_import_legacy_package() -> None:
    violations = []
    for path in SOURCE_ROOT.rglob("*.py"):
        violations.extend(
            f"{path}:{name}"
            for name in _imports(path)
            if name == "stock_analyzer" or name.startswith("stock_analyzer.")
        )
    assert violations == []


def test_active_source_files_do_not_exceed_1200_lines() -> None:
    source_suffixes = {".py", ".js", ".css", ".html"}
    violations = {
        str(path.relative_to(SOURCE_ROOT)): len(path.read_text(encoding="utf-8").splitlines())
        for path in SOURCE_ROOT.rglob("*")
        if path.is_file()
        and path.suffix in source_suffixes
        and len(path.read_text(encoding="utf-8").splitlines()) > 1200
    }

    assert violations == {}


def test_active_dependency_direction() -> None:
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
        "application/today_scheduler_runtime.py",
        "application/tomorrow_scheduler_runtime.py",
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


def test_active_web_surface_and_fixed_long_tabs_use_unified_routes() -> None:
    web = SOURCE_ROOT / "web"
    api = web / "api"
    assert {"routes.py", "route_services.py", "decision_serializers.py", "decision_sse.py"} <= {
        path.name for path in api.glob("*.py")
    }
    assert not any(
        (web / name).exists()
        for name in ("routes.py", "routes_v2.py", "route_services.py", "decision_serializers.py", "decision_sse.py")
    )
    dashboard = (web / "static/dashboard.js").read_text(encoding="utf-8")
    groups = (web / "static/long_groups.js").read_text(encoding="utf-8")
    assert "/api/decisions/" in dashboard
    assert "/api/v2/" not in dashboard
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
    assert "def publish_overlay_event(overlay: DecisionOverlay)" in source
    assert "parent_content_hash=current.content_hash" in source
    assert "publish_overlay=publish_overlay_event" in source


def test_overlay_publisher_and_input_quality_are_required_typed_runtime_boundaries() -> None:
    publisher = next(field for field in fields(RuntimeDependencies) if field.name == "publish_overlay")

    assert publisher.default is MISSING
    assert "input_quality_status" in DecisionBuilderPort.__dict__


def test_internal_state_is_typed_until_an_explicit_observability_boundary() -> None:
    violations: list[str] = []
    conversion_names = {"as_dict", "to_json", "to_status"}
    exempt_status_paths = {
        Path("application/ports/reviews.py"),
        Path("application/ports/market.py"),
        Path("infra/deepseek/reviewer.py"),
        Path("infra/deepseek/reviewer_status.py"),
        Path("infra/market_data/service/facade.py"),
        Path("infra/market_data/service/service_health.py"),
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
            if node.name not in {"health", "status"} or relative in exempt_status_paths:
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


def test_identity_and_audit_payloads_have_one_explicit_field_projection() -> None:
    identity = (SOURCE_ROOT / "domain/recommendation/decision_identity.py").read_text(encoding="utf-8")
    codec = (SOURCE_ROOT / "infra/persistence/decision_record_codec.py").read_text(encoding="utf-8")
    audit = (SOURCE_ROOT / "application/research/research_audit.py").read_text(encoding="utf-8")
    columnar = (SOURCE_ROOT / "infra/market_data/normalization/columnar.py").read_text(encoding="utf-8")

    assert "def committed_record_identity_payload(" in identity
    assert "committed_record_identity_payload(record)" in codec
    for duplicate in (
        "def _record_payload(",
        "def _decision_payload(",
        "def _decision_item_payload(",
        "def _downside_payload(",
        "def _research_coverage_payload(",
        "def _selection_diagnostics_payload(",
        "def _decision_quote_payload(",
    ):
        assert duplicate not in codec
    assert ".__dict__" not in audit
    assert ".__dict__" not in columnar


def test_tomorrow_holdout_serializer_uses_an_explicit_public_field_whitelist() -> None:
    paths = (SOURCE_ROOT / "infra/research/tomorrow_profile_holdout_artifacts.py",)
    violations: list[str] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in {"asdict", "__dict__"}:
                violations.append(f"{path.relative_to(SOURCE_ROOT)}:{node.lineno}: automatic field projection")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "vars":
                violations.append(f"{path.relative_to(SOURCE_ROOT)}:{node.lineno}: automatic field projection")

    assert violations == []


def test_runtime_responsibilities_remain_split_by_resource_boundary() -> None:
    input_runtime = (SOURCE_ROOT / "application/market_data/input_runtime.py").read_text(encoding="utf-8")
    decision_adapters = (SOURCE_ROOT / "application/decisions/decision_adapters.py").read_text(encoding="utf-8")
    runtime = (SOURCE_ROOT / "application/runtime/scheduler_runtime.py").read_text(encoding="utf-8")
    issues = (SOURCE_ROOT / "application/runtime/runtime_issues.py").read_text(encoding="utf-8")

    assert "class DeepSeekAdapter" not in input_runtime
    assert "class FreezeAdapter" not in input_runtime
    assert "class DeepSeekAdapter" in decision_adapters
    assert "class FreezeAdapter" in decision_adapters
    assert "class RuntimeIssueRegistry" not in runtime
    assert "class RuntimeIssueRegistry" in issues


def test_market_component_suite_remains_partitioned_by_behavior() -> None:
    component_root = PROJECT_ROOT / "tests/component"
    assert not (component_root / "test_market_data.py").exists()
    expected = {
        "test_market_features.py",
        "test_market_exchange_references.py",
        "test_market_gateway.py",
        "test_market_history.py",
        "test_market_intraday.py",
        "test_market_lanes.py",
        "test_market_references.py",
        "test_market_research.py",
        "test_market_service.py",
        "test_market_tushare.py",
        "test_market_vendors.py",
    }
    assert expected <= {path.name for path in component_root.glob("test_market_*.py")}
    assert all(len((component_root / name).read_text(encoding="utf-8").splitlines()) < 1200 for name in expected)


def test_dashboard_stream_transport_is_a_separate_packaged_dependency() -> None:
    dashboard = (SOURCE_ROOT / "web/static/dashboard.js").read_text(encoding="utf-8")
    stream = (SOURCE_ROOT / "web/static/dashboard_stream.js").read_text(encoding="utf-8")
    template = (SOURCE_ROOT / "web/templates/index.html").read_text(encoding="utf-8")

    assert "new EventSource" not in dashboard
    assert "new EventSource" in stream
    assert "dashboard_stream.js" in template
    assert template.index("dashboard_stream.js") < template.index("dashboard.js")


def test_functional_package_final_cutover_contract_is_authoritative() -> None:
    design = PROJECT_ROOT / "docs/software-business-design.md"
    content = design.read_text(encoding="utf-8")
    assert "### 3.1 功能包目标布局与迁移约束" in content
    assert "最终包状态已固化" in content
    assert "根级迁移路径不属于活动树" in content
    assert not (PROJECT_ROOT / "docs/plan.md").exists()


def test_domain_and_application_do_not_own_persistence_or_json_decoders() -> None:
    violations: list[str] = []
    forbidden_public_codecs = {
        "committed_record_bytes",
        "committed_record_from_bytes",
    }
    for boundary in ("domain", "application"):
        for path in (SOURCE_ROOT / boundary).rglob("*.py"):
            relative = path.relative_to(SOURCE_ROOT)
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in forbidden_public_codecs:
                    violations.append(f"{relative}:{node.lineno}: persistence codec belongs in infra")
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if (
                    isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "json"
                    and node.func.attr
                    in {
                        "load",
                        "loads",
                    }
                ):
                    violations.append(f"{relative}:{node.lineno}: JSON decoding belongs at an explicit adapter")
    assert violations == []


def test_production_composition_injects_the_single_cadence_planner() -> None:
    source = (SOURCE_ROOT / "bootstrap.py").read_text(encoding="utf-8")

    assert "CadencePlanner(" in source
    assert "cadence=cadence_planner" in source
