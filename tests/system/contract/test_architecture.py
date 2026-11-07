"""Architecture checks and counterexamples that exercise those checks."""

from __future__ import annotations

import ast
import subprocess
from dataclasses import fields
from functools import cache
from pathlib import Path

import pytest

from tests.helpers.shared.module_boundaries import (
    BusinessSourceFiles,
    CoreIoViolations,
    ImportCycles,
    LayerImportViolations,
    ParseSourceImports,
    ResourceCreationViolations,
    SourceBoundaryViolations,
    SourceImportGraph,
    SourceImports,
    SourceModuleName,
    SourceImportTargets,
)
from tests.helpers.shared.python_source import source_text, source_tree
from trader.recommendation.application.ports.runtime import FreezeBuilderPort
from trader.recommendation.application.runtime.runtime_dependencies import RuntimeDependencies


SOURCE_ROOT = Path(__file__).resolve().parents[3] / "src" / "trader"
PROJECT_ROOT = SOURCE_ROOT.parents[1]


def test_native_sqlite_statements_are_owned_only_by_the_connection_primitive() -> None:
    forbidden = []
    for path in (*SOURCE_ROOT.rglob("*.py"), *(PROJECT_ROOT / "scripts").rglob("*.py")):
        if path == SOURCE_ROOT / "infra/sqlite_connection.py":
            continue
        for node in ast.walk(source_tree(path)):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            native_connection = (
                isinstance(node.func.value, ast.Name)
                and node.func.value.id == "sqlite3"
                and node.func.attr == "connect"
            )
            native_statement = node.func.attr in {"executemany", "executescript"} or (
                node.func.attr == "execute"
                and bool(node.args)
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and node.args[0].value.lstrip().split(" ", 1)[0].upper()
                in {"SELECT", "INSERT", "UPDATE", "DELETE", "PRAGMA", "CREATE", "BEGIN"}
            )
            if native_connection or native_statement:
                forbidden.append(f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}")
    assert forbidden == []


def test_product_tree_uses_precise_resource_and_state_names() -> None:
    prohibited = "life" + "cycle"
    tracked = (
        subprocess.run(
            ["git", "ls-files", "-z", "src/trader"],
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
            content = source_text(path)
        except (UnicodeDecodeError, OSError):
            continue
        if prohibited in content.casefold():
            content_violations.append(relative)

    assert path_violations == []
    assert content_violations == []


@cache
def _imports(path: Path) -> frozenset[str]:
    return SourceImportTargets(path, SOURCE_ROOT)


def test_product_code_does_not_import_legacy_package() -> None:
    violations = []
    for path in SOURCE_ROOT.rglob("*.py"):
        violations.extend(
            f"{path}:{name}"
            for name in _imports(path)
            if name == "stock_analyzer" or name.startswith("stock_analyzer.")
        )
    assert violations == []


def test_business_domains_delegate_json_representation_to_codecs() -> None:
    violations = [
        str(path.relative_to(SOURCE_ROOT))
        for business in ("download", "training", "recommendation")
        for path in (SOURCE_ROOT / business / "domain").rglob("*.py")
        if any(name == "json" or name.startswith("json.") for name in _imports(path))
    ]
    assert violations == []


def test_source_files_do_not_exceed_1500_lines() -> None:
    source_suffixes = {".py", ".js", ".css", ".html"}
    violations = {
        str(path.relative_to(SOURCE_ROOT)): line_count
        for path in SOURCE_ROOT.rglob("*")
        if path.is_file()
        and path.suffix in source_suffixes
        and (line_count := len(source_text(path).splitlines())) > 1500
    }

    assert violations == {}


def test_product_dependency_direction() -> None:
    BusinessSourceFiles(SOURCE_ROOT, "domain")
    BusinessSourceFiles(SOURCE_ROOT, "application")
    assert SourceBoundaryViolations(SOURCE_ROOT) == ()


def test_shared_technical_primitives_have_one_infra_owner() -> None:
    owners = {
        "ShanghaiClock": SOURCE_ROOT / "infra/clock/shanghai.py",
        "RuntimeSettings": SOURCE_ROOT / "infra/settings/models.py",
        "AtomicWriteJson": SOURCE_ROOT / "infra/atomic_files/json.py",
        "BoundedLruCache": SOURCE_ROOT / "infra/cache.py",
        "BoundedExecutor": SOURCE_ROOT / "infra/workers.py",
        "ProcessLock": SOURCE_ROOT / "infra/process_lock.py",
        "RuntimeWorkerResources": SOURCE_ROOT / "recommendation/infra/runtime/application_system.py",
        "ShutdownDeadline": SOURCE_ROOT / "infra/shutdown.py",
    }
    assert all(path.is_file() for path in owners.values())

    definitions: dict[str, list[Path]] = {name: [] for name in owners}
    for path in SOURCE_ROOT.rglob("*.py"):
        tree = source_tree(path)
        for node in tree.body:
            name = node.name if isinstance(node, (ast.ClassDef, ast.FunctionDef)) else None
            if name in definitions:
                definitions[name].append(path.relative_to(SOURCE_ROOT))
    assert definitions == {name: [path.relative_to(SOURCE_ROOT)] for name, path in owners.items()}


def test_execution_resources_are_created_only_by_the_composition_root() -> None:
    violations: list[str] = []
    for path in SOURCE_ROOT.rglob("*.py"):
        relative = path.relative_to(SOURCE_ROOT)
        violations.extend(
            f"{relative}:{issue}" for issue in ResourceCreationViolations(
                source_text(path), SourceModuleName(path, SOURCE_ROOT), is_package=path.name == "__init__.py",
            )
        )
    assert violations == []


def test_shared_market_data_primitives_do_not_import_recommendation() -> None:
    violations = [
        f"{path.relative_to(SOURCE_ROOT)} -> {imported}"
        for path in (SOURCE_ROOT / "infra/market_data").rglob("*.py")
        for imported in _imports(path)
        if imported.startswith("trader.recommendation")
    ]
    assert violations == []


def test_application_does_not_own_infrastructure_implementations() -> None:
    forbidden_names = {
        "BoundedLruCache",
        "BoundedExecutor",
        "ProcessLock",
        "RuntimeWorkerResources",
        "RuntimeJsonWriter",
        "AtomicReadJson",
        "AtomicWriteJson",
        "ShanghaiClock",
        "ShutdownDeadline",
    }
    violations: list[str] = []
    for path in BusinessSourceFiles(SOURCE_ROOT, "application"):
        tree = source_tree(path)
        for node in ast.walk(tree):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in forbidden_names:
                violations.append(f"{path.relative_to(SOURCE_ROOT)}:{node.lineno}:{node.name}")
    assert violations == []


def test_model_scoring_router_is_profile_agnostic_and_replaces_tomorrow_specific_injection() -> None:
    port = SOURCE_ROOT / "recommendation/application/ports/loaded_profile.py"
    router = SOURCE_ROOT / "recommendation/application/pipeline/local_score/model_router.py"
    projection = source_text(SOURCE_ROOT / "recommendation/application/pipeline/final_selection/freeze_projection.py")
    input_runtime = source_text(SOURCE_ROOT / "recommendation/application/pipeline/data_source/source_router.py")

    assert not any(name.startswith("trader.application.recommendation") for name in _imports(port))
    router_source = source_text(router)
    assert all(profile not in router_source for profile in ('"v1"', '"v2"', '"v3"'))
    assert "tomorrow_model:" not in projection
    assert "tomorrow_model:" not in input_runtime
    assert "model_scoring: ModelScoringPort" in projection
    assert "model_scoring: ModelScoringPort" in input_runtime


def test_v1_scoring_profile_is_retired() -> None:
    v1_root = SOURCE_ROOT / "recommendation/infra/scoring/profiles/v1"
    factory = source_text(SOURCE_ROOT / "recommendation/infra/scoring/profile_factory.py")

    assert not any(v1_root.rglob("*.py"))
    assert "build_v1_profile" not in factory
    assert not (SOURCE_ROOT / "infra/tomorrow_production_model.py").exists()


def test_v2_and_v3_profiles_share_one_neutral_trained_head_owner() -> None:
    v2_root = SOURCE_ROOT / "training/infra/profile/v2"
    v3_root = SOURCE_ROOT / "training/infra/profile/v3"
    shared_root = SOURCE_ROOT / "training/infra/model_bundles"
    assert not (SOURCE_ROOT / "training/infra/artifacts").exists()
    model_port = source_text(SOURCE_ROOT / "recommendation/application/ports/loaded_profile.py")
    factory = source_text(SOURCE_ROOT / "recommendation/infra/scoring/profile_factory.py")

    for relative in (
        "bundle_codec.py",
        "bundle_snapshot.py",
        "profile_publication.py",
        "contracts.py",
        "predictor.py",
        "profile.py",
    ):
        assert (shared_root / relative).is_file()
    for retired in (
        v2_root / "artifact_codec.py",
        v2_root / "model.json",
        v2_root / "profile.py",
        v2_root / "heads/tomorrow/predictor.py",
        v3_root / "bundle_codec.py",
        v3_root / "bundle_locator.py",
        v3_root / "profile.py",
        v3_root / "training_bundle_repository.py",
        v3_root / "training_contracts.py",
        v3_root / "heads/inference.py",
    ):
        assert not retired.exists()
    assert "TomorrowHistoricalModelSnapshot" not in model_port
    assert "ModelPredictorPort" in model_port
    assert "BuildTrainedScoringProfile" in factory
    assert not (SOURCE_ROOT / "recommendation/application/ports/tomorrow_model.py").exists()


def test_v3_profile_keeps_only_offline_training_implementation() -> None:
    v3_root = SOURCE_ROOT / "training/infra/profile/v3"
    shared_training_root = SOURCE_ROOT / "training/infra"

    assert not (SOURCE_ROOT / "infra/tomorrow_production_model.py").exists()
    offline_training_modules = {
        "sample_builder.py",
        "training.py",
        "training_sample_index.py",
    }
    assert all((v3_root / name).is_file() for name in offline_training_modules)
    assert (shared_training_root / "model_fitting.py").is_file()
    assert not (v3_root / "model_fitting.py").exists()
    assert not any((v3_root / "heads").rglob("*.py"))


def test_old_production_chain_has_no_source_files() -> None:
    retired = (
        "application/candidate_features.py",
        "application/pipeline.py",
        "application/published_snapshots.py",
        "application/publisher.py",
        "application/queries.py",
        "application/recommendation_replay.py",
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


def test_web_surface_and_fixed_long_tabs_use_unified_routes() -> None:
    web = SOURCE_ROOT / "web"
    api = SOURCE_ROOT / "http_api"
    assert {"route_services.py"} <= {path.name for path in api.glob("*.py")}
    assert (api / "response" / "freeze_projection.py").is_file()
    assert (api / "handlers" / "product_handler.py").is_file()
    assert (api / "routes" / "page_routes.py").is_file()
    assert not any((web / "api").glob("*.py"))
    assert not any(
        (web / name).exists()
        for name in ("routes.py", "routes_v2.py", "route_services.py", "freeze_serializers.py", "freeze_sse.py")
    )
    dashboard = source_text(web / "static/dashboard.js")
    groups = source_text(web / "static/long_groups.js")
    assert "/api/freezes/" in dashboard
    assert "/api/v2/" not in dashboard
    assert "/api/recommendations/" not in dashboard
    assert '"chokepoint"' in groups
    assert '"future_growth"' in groups
    assert '"low_price_potential"' in groups


def test_domain_has_no_external_io_or_clock_reads() -> None:
    violations = []
    for path in BusinessSourceFiles(SOURCE_ROOT, "domain"):
        violations.extend(
            f"{path.relative_to(SOURCE_ROOT)}:{issue}"
            for issue in CoreIoViolations(path.read_text(encoding="utf-8"), "domain")
        )
    assert violations == []


def test_bootstrap_is_the_only_composition_root() -> None:
    test_execution_resources_are_created_only_by_the_composition_root()
    definitions = []
    for path in SOURCE_ROOT.rglob("*.py"):
        for node in source_tree(path).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in {"BuildSystem", "BuildProfiledSystem"}:
                definitions.append((node.name, path.relative_to(SOURCE_ROOT)))
    assert sorted(definitions) == [("BuildProfiledSystem", Path("bootstrap.py")), ("BuildSystem", Path("bootstrap.py"))]


@pytest.mark.parametrize("source", (
    "from trader.infra.workers import BoundedExecutor as Executor\nExecutor()",
    "import trader.infra.workers as workers\nworkers.BoundedExecutor()",
    "import concurrent.futures\nconcurrent.futures.ThreadPoolExecutor()",
    "from concurrent import futures as pool\npool.ProcessPoolExecutor()",
    "from trader.infra.workers import BoundedExecutor\nFactory = BoundedExecutor\nFactory()",
    "from trader.infra.http_transport import RequestsHttpTransport as Client\nClient()",
    "from trader.infra.isolated_http import IsolatedHttpExecutor as HttpPool\nHttpPool()",
    "from trader.infra.clock import shanghai as clock\nclock.ShanghaiClock()",
))
def test_resource_creation_gate_rejects_aliased_and_attribute_constructors(source) -> None:
    assert ResourceCreationViolations(source, "trader.recommendation.infra.second_root")


def test_resource_creation_gate_keeps_injection_and_same_named_values_legal() -> None:
    assert ResourceCreationViolations(
        "from trader.infra.workers import BoundedExecutor as Executor\nExecutor()", "trader.bootstrap",
    ) == ()
    assert ResourceCreationViolations(
        "from concurrent import futures\nfutures.ThreadPoolExecutor()", "trader.infra.workers",
    ) == ()
    assert ResourceCreationViolations(
        "def use(Executor):\n    return Executor()", "trader.training.infra.consumer",
    ) == ()


@pytest.mark.parametrize("business", ("download", "training", "recommendation"))
@pytest.mark.parametrize("layer", ("domain", "application", "infra"))
def test_shared_infra_cannot_import_business_values_or_implementations(business, layer) -> None:
    module = "trader.infra.example"
    source = f"from trader.{business}.{layer}.example import Value"
    assert LayerImportViolations(module, ParseSourceImports(source, module))


def test_application_runtime_modules_are_reachable_from_bootstrap() -> None:
    graph = SourceImportGraph(SOURCE_ROOT)
    runtime_modules = {module for module in graph if module.startswith("trader.recommendation.application.runtime.")}
    assert runtime_modules
    reachable: set[str] = set()
    pending = ["trader.bootstrap"]
    while pending:
        module = pending.pop()
        if module not in reachable:
            reachable.add(module)
            pending.extend(graph[module])
    assert runtime_modules <= reachable


def test_bootstrap_does_not_wire_retired_shortline_overlay_events() -> None:
    source = source_text(SOURCE_ROOT / "bootstrap.py")
    assert "def publish_overlay_event(overlay: FreezeOverlay)" not in source
    assert "publish_overlay=publish_overlay_event" not in source


def test_retired_overlay_boundary_is_inert_and_input_quality_remains_typed() -> None:
    publisher = next(field for field in fields(RuntimeDependencies) if field.name == "publish_overlay")

    assert publisher.default is None
    assert "input_quality_status" in FreezeBuilderPort.__dict__


def test_internal_state_is_typed_until_an_explicit_observability_boundary() -> None:
    violations: list[str] = []
    conversion_names = {"as_dict", "to_json", "to_status"}
    exempt_status_paths = {
        Path("recommendation/application/ports/deepseek.py"),
        Path("recommendation/application/ports/market_data.py"),
        Path("recommendation/infra/deepseek/reviewer.py"),
        Path("recommendation/infra/deepseek/reviewer_status.py"),
        Path("recommendation/infra/market_data/market_feature_service.py"),
        Path("recommendation/infra/market_data/market_data_health.py"),
    }
    trader_root = SOURCE_ROOT
    forbidden_status_types = {"Any", "JsonObject", "JsonValue", "Mapping", "MutableMapping", "dict", "object"}
    for path in trader_root.rglob("*.py"):
        relative = path.relative_to(trader_root)
        tree = source_tree(path)
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
    identity = source_text(SOURCE_ROOT / "recommendation/domain/publication/freeze_identity.py")
    codec = source_text(SOURCE_ROOT / "recommendation/infra/persistence/freeze_record_codec.py")
    audit = source_text(SOURCE_ROOT / "training/application/research_audit.py")
    columnar = source_text(SOURCE_ROOT / "recommendation/infra/normalization/columnar.py")

    assert "def CommittedRecordIdentityPayload(" in identity
    assert "CommittedRecordIdentityPayload(record)" in codec
    for duplicate in (
        "def _record_payload(",
        "def _freeze_payload(",
        "def _freeze_item_payload(",
        "def _downside_payload(",
        "def _research_coverage_payload(",
        "def _selection_diagnostics_payload(",
        "def _freeze_quote_payload(",
    ):
        assert duplicate not in codec
    assert ".__dict__" not in audit
    assert ".__dict__" not in columnar


def test_tomorrow_holdout_serializer_uses_an_explicit_public_field_whitelist() -> None:
    paths = (
        SOURCE_ROOT / "training/infra/research/point_in_time_dataset_evidence.py",
        SOURCE_ROOT / "training/infra/research/tomorrow_point_in_time_holdout_reports.py",
    )
    violations: list[str] = []
    for path in paths:
        tree = source_tree(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in {"asdict", "__dict__"}:
                violations.append(f"{path.relative_to(SOURCE_ROOT)}:{node.lineno}: automatic field projection")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "vars":
                violations.append(f"{path.relative_to(SOURCE_ROOT)}:{node.lineno}: automatic field projection")

    assert violations == []


def test_runtime_responsibilities_remain_split_by_resource_boundary() -> None:
    input_runtime = source_text(SOURCE_ROOT / "recommendation/application/pipeline/data_source/source_router.py")
    freeze_adapters = source_text(SOURCE_ROOT / "recommendation/application/pipeline/freeze_publish/runtime_adapters.py")
    runtime = source_text(SOURCE_ROOT / "recommendation/application/runtime/scheduler_runtime.py")
    issues = source_text(SOURCE_ROOT / "recommendation/application/runtime/runtime_issues.py")

    assert "class DeepSeekAdapter" not in input_runtime
    assert "class FreezeAdapter" not in input_runtime
    assert "class DeepSeekAdapter" in freeze_adapters
    assert "class FreezeAdapter" in freeze_adapters
    assert "class RuntimeIssueIndex" not in runtime
    assert "class RuntimeIssueIndex" in issues


def test_market_component_suite_remains_partitioned_by_behavior() -> None:
    component_root = PROJECT_ROOT / "tests/recommendation/component/market"
    download_root = PROJECT_ROOT / "tests/download/component/market"
    assert not (component_root / "test_market_data.py").exists()
    expected = {
        "test_market_features.py",
        "test_market_gateway.py",
        "test_market_intraday.py",
        "test_market_lanes.py",
        "test_market_references.py",
        "test_market_service.py",
        "test_market_vendors.py",
    }
    assert expected <= {path.name for path in component_root.glob("test_market_*.py")}
    assert (PROJECT_ROOT / "tests/download/component/company/test_company_evidence.py").is_file()
    assert (download_root / "test_market_tushare.py").is_file()
    assert all(len(source_text(component_root / name).splitlines()) < 1200 for name in expected)
    assert len(source_text(download_root / "test_market_tushare.py").splitlines()) < 1200


def test_dashboard_stream_transport_is_a_separate_packaged_dependency() -> None:
    dashboard = source_text(SOURCE_ROOT / "web/static/dashboard.js")
    stream = source_text(SOURCE_ROOT / "web/static/dashboard_stream.js")
    template = source_text(SOURCE_ROOT / "web/templates/index.html")

    assert "new EventSource" not in dashboard
    assert "new EventSource" in stream
    assert "dashboard_stream.js" in template
    assert template.index("dashboard_stream.js") < template.index("dashboard.js")


def test_domain_and_application_do_not_own_persistence_or_json_decoders() -> None:
    violations: list[str] = []
    forbidden_public_codecs = {
        "CommittedRecordBytes",
        "CommittedRecordFromBytes",
    }
    for boundary in ("domain", "application"):
        for path in BusinessSourceFiles(SOURCE_ROOT, boundary):
            relative = path.relative_to(SOURCE_ROOT)
            tree = source_tree(path)
            violations.extend(
                f"{relative}:{issue}" for issue in CoreIoViolations(path.read_text(encoding="utf-8"), boundary)
            )
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in forbidden_public_codecs:
                    violations.append(f"{relative}:{node.lineno}: persistence codec belongs in infra")
    assert violations == []


def test_history_month_values_and_sqlite_codec_keep_serialization_at_the_infra_boundary() -> None:
    domain_path = SOURCE_ROOT / "download/domain/history_revision.py"
    codec_path = SOURCE_ROOT / "download/infra/history_revision_codec.py"
    partition_path = SOURCE_ROOT / "download/infra/history_prices.py"
    domain_source = source_text(domain_path)
    codec_source = source_text(codec_path)
    partition_source = source_text(partition_path)

    assert "import json" not in domain_source
    assert "import sqlite3" not in domain_source
    assert "import json" not in codec_source
    assert "RevisionFromColumns" in codec_source
    assert "PriceColumnValues" in codec_source
    assert "import sqlite3" in partition_source
    assert "history_revision_codec" in partition_source
    for automatic_projection in ("asdict(", ".__dict__", "vars("):
        assert automatic_projection not in codec_source


def test_history_sync_progress_stays_typed_until_the_entrypoint_projection() -> None:
    contract = source_text(SOURCE_ROOT / "download/domain/history_sync.py")
    supplier = source_text(SOURCE_ROOT / "download/infra/baostock_sync_supplier.py")
    runtime = source_text(SOURCE_ROOT / "download/application/download_history.py")
    io_source = source_text(SOURCE_ROOT / "download/infra/history_sync_io.py")
    projection = source_text(SOURCE_ROOT / "download/entrypoints/history_sync_progress.py")

    assert "class HistorySyncProgress:" in contract
    assert "class HistorySyncProgressPort(Protocol):" in contract
    assert all("import json" not in source for source in (contract, supplier, runtime, io_source))
    assert "import json" not in projection
    assert "def publish_result(" in projection
    assert "总进度" in projection
    assert "尝试" in projection
    terminal = source_text(SOURCE_ROOT / "download/infra/terminal_progress.py")
    assert "TerminalDownloadProgress" in projection
    assert "sys.stderr" in terminal
    assert "self._stream.flush()" in terminal
    assert 'self._stream.write(line + "\\n")' in terminal


def test_download_business_has_one_owner_and_no_legacy_history_paths() -> None:
    download = SOURCE_ROOT / "download"
    assert all((download / relative).is_dir() for relative in ("domain", "application", "infra", "entrypoints"))
    assert not any((SOURCE_ROOT / "application" / "history").glob("*.py"))
    retired = (
        "baostock_gap_supplier.py",
        "baostock_gateway.py",
        "baostock_session.py",
        "baostock_sync_supplier.py",
        "history_sync_runner.py",
        "history_control_repository.py",
        "history_prices.py",
    )
    assert not any((SOURCE_ROOT / "infra" / "research" / name).exists() for name in retired)
    assert not (download / "infra" / "baostock_gap_supplier.py").exists()
    assert not (download / "infra" / "history_sync_runner.py").exists()
    assert not (download / "infra" / "history_sync_gateway.py").exists()


def test_production_composition_injects_the_single_cadence_planner() -> None:
    source = source_text(SOURCE_ROOT / "bootstrap.py")

    assert "CadencePlanner(" in source
    assert "cadence=cadence_planner" in source


def _write_source(root: Path, relative: str, source: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def _business_sources(root: Path) -> None:
    for business in ("download", "training", "recommendation"):
        for layer in ("domain", "application"):
            _write_source(root, f"{business}/{layer}/values.py", "VALUE = 1\n")


@pytest.mark.parametrize("business", ("download", "training", "recommendation"))
@pytest.mark.parametrize("layer", ("domain", "application"))
@pytest.mark.parametrize(
    "source",
    (
        "import trader.BUSINESS.infra.adapter as implementation",
        "from trader.BUSINESS import infra as implementation",
        "from ..infra import adapter as implementation",
        "from .. import infra",
        "def execute():\n    from ..infra import adapter",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from ..infra import Adapter",
    ),
)
def test_dependency_gate_rejects_implementation_imports(tmp_path, monkeypatch, business, layer, source) -> None:
    root = tmp_path / "trader"
    _business_sources(root)
    _write_source(root, f"{business}/{layer}/poison.py", source.replace("BUSINESS", business))
    monkeypatch.setattr(__name__ + ".SOURCE_ROOT", root)

    with pytest.raises(AssertionError, match="poison.py"):
        test_product_dependency_direction()


@pytest.mark.parametrize("business", ("download", "training", "recommendation"))
@pytest.mark.parametrize("layer", ("domain", "application"))
def test_dependency_gate_rejects_other_business_implementations(business, layer) -> None:
    module = f"trader.{business}.{layer}.example"
    other = "training" if business != "training" else "download"
    source = f"from trader.{other}.infra.adapter import Adapter"

    assert LayerImportViolations(module, ParseSourceImports(source, module))


@pytest.mark.parametrize(
    "target",
    (
        "trader.training.application.tomorrow_training",
        "trader.training.domain.model_publication",
        "trader.download.application.read_published_history",
    ),
)
def test_recommendation_application_cannot_expand_the_publication_handoff(target) -> None:
    module = "trader.recommendation.application.example"
    source = f"from {target} import Value"

    assert LayerImportViolations(module, ParseSourceImports(source, module))


@pytest.mark.parametrize(
    "module",
    (
        "trader.recommendation.application.ports.loaded_profile",
        "trader.recommendation.application.pipeline.local_score.model_capability",
    ),
)
def test_existing_publication_value_handoff_remains_legal(module) -> None:
    source = "from trader.training.domain.model_publication import HeadTrainingStatus"

    assert LayerImportViolations(module, ParseSourceImports(source, module)) == ()


@pytest.mark.parametrize("member", ("SqliteActivity", "SqliteObservation", "IndependentSqliteWork"))
def test_recommendation_accepts_only_scoped_database_counters(member) -> None:
    module = "trader.recommendation.application.runtime.example"
    source = f"from trader.infra.sqlite_observation import {member}"
    assert LayerImportViolations(module, ParseSourceImports(source, module)) == ()


@pytest.mark.parametrize("source", (
    "from trader.infra.sqlite_observation import SqliteSession",
    "import trader.infra.sqlite_observation",
    "from trader.infra.sqlite_observation import *",
    "from trader.infra.sqlite_connection import SqliteSession",
))
def test_database_observation_does_not_grant_database_access(source) -> None:
    module = "trader.recommendation.application.runtime.example"
    assert LayerImportViolations(module, ParseSourceImports(source, module))


def test_core_cannot_hide_imports_behind_a_wildcard() -> None:
    module = "trader.training.domain.example"
    source = "from trader.infra.serialization.canonical_codec import *"

    assert LayerImportViolations(module, ParseSourceImports(source, module))


def test_dependency_gate_accepts_pure_business_values(tmp_path, monkeypatch) -> None:
    root = tmp_path / "trader"
    _business_sources(root)
    _write_source(
        root,
        "training/application/replay.py",
        "from trader.download.domain.published_history import PublishedHistory\n"
        "from trader.recommendation.domain.publication.models import Strategy\n",
    )
    monkeypatch.setattr(__name__ + ".SOURCE_ROOT", root)

    test_product_dependency_direction()


@pytest.mark.parametrize("layer", ("domain", "application"))
@pytest.mark.parametrize("empty_package", (False, True))
def test_business_scan_rejects_missing_or_empty_boundaries(tmp_path, layer, empty_package) -> None:
    root = tmp_path / "trader"
    if empty_package:
        for business in ("download", "training", "recommendation"):
            _write_source(root, f"{business}/{layer}/__init__.py", "")

    with pytest.raises(AssertionError, match=f"download/{layer}"):
        BusinessSourceFiles(root, layer)


@pytest.mark.parametrize("business", ("download", "training", "recommendation"))
@pytest.mark.parametrize(
    "source",
    (
        "import sqlite3 as driver\n",
        "from datetime import datetime as Moment\nVALUE = Moment.now()\n",
        "VALUE = open('local-file')\n",
        "from pathlib import Path\nVALUE = Path('local-file').read_text()\n",
    ),
)
def test_domain_io_gate_reads_each_business_domain(tmp_path, monkeypatch, business, source) -> None:
    root = tmp_path / "trader"
    _business_sources(root)
    _write_source(root, f"{business}/domain/poison.py", source)
    monkeypatch.setattr(__name__ + ".SOURCE_ROOT", root)

    with pytest.raises(AssertionError, match="poison.py"):
        test_domain_has_no_external_io_or_clock_reads()


@pytest.mark.parametrize("business", ("download", "training", "recommendation"))
@pytest.mark.parametrize("layer", ("domain", "application"))
@pytest.mark.parametrize(
    "source",
    (
        "import json\nVALUE = json.loads('{}')\n",
        "import json as codec\nVALUE = codec.loads('{}')\n",
        "from json import loads as Decode\nVALUE = Decode('{}')\n",
    ),
)
def test_json_decoder_gate_reads_each_business_core(tmp_path, monkeypatch, business, layer, source) -> None:
    root = tmp_path / "trader"
    _business_sources(root)
    _write_source(root, f"{business}/{layer}/poison.py", source)
    monkeypatch.setattr(__name__ + ".SOURCE_ROOT", root)

    with pytest.raises(AssertionError, match="JSON decoding"):
        test_domain_and_application_do_not_own_persistence_or_json_decoders()


@pytest.mark.parametrize("owner", ("http_api", "web"))
@pytest.mark.parametrize(
    "target",
    (
        "trader.bootstrap",
        "trader.download.application.download_history",
        "trader.training.infra.engine",
        "trader.recommendation.infra.persistence.freeze_records",
        "trader.recommendation.application.pipeline.local_score.base_scoring",
    ),
)
def test_delivery_gate_rejects_business_execution_and_infrastructure(owner, target) -> None:
    module = f"trader.{owner}.example"
    assert LayerImportViolations(module, ParseSourceImports(f"import {target} as implementation", module))


@pytest.mark.parametrize("owner", ("http_api", "web"))
def test_delivery_gate_accepts_read_only_queries_and_domain_values(owner) -> None:
    module = f"trader.{owner}.example"
    source = (
        "from trader.recommendation.application.pipeline.freeze_publish.read_only_queries import UnifiedFreezeQueries\n"
        "from trader.recommendation.domain.publication.models import Strategy\n"
    )
    assert LayerImportViolations(module, ParseSourceImports(source, module)) == ()


def test_relative_imports_resolve_package_initializers_and_members(tmp_path) -> None:
    root = tmp_path / "trader"
    path = _write_source(root, "training/application/__init__.py", "from .. import infra as implementation\n")

    assert SourceImportTargets(path, root) == {"trader.training", "trader.training.infra"}
    assert SourceImports(path, root)[0].line == 1


def test_import_graph_detects_transitive_cycles_and_module_members(tmp_path) -> None:
    root = tmp_path / "trader"
    _write_source(root, "training/application/first.py", "from . import second\n")
    _write_source(root, "training/application/second.py", "from .third import Value\n")
    _write_source(root, "training/application/third.py", "import trader.training.application.first as first\n")

    cycles = ImportCycles(SourceImportGraph(root, import_time_only=True))

    assert cycles == (
        (
            "trader.training.application.first",
            "trader.training.application.second",
            "trader.training.application.third",
            "trader.training.application.first",
        ),
    )


def test_package_initializers_are_included_without_artificial_ancestor_cycles(tmp_path) -> None:
    root = tmp_path / "trader"
    _write_source(root, "training/__init__.py", "from . import first\n")
    _write_source(root, "training/first.py", "from .second import Value\n")
    _write_source(root, "training/second.py", "VALUE = 1\n")
    assert ImportCycles(SourceImportGraph(root, import_time_only=True)) == ()


def test_external_package_initializers_can_close_an_import_cycle(tmp_path) -> None:
    root = tmp_path / "trader"
    _write_source(root, "download/application/first.py", "from trader.training.domain.values import Value\n")
    _write_source(root, "training/__init__.py", "import trader.download.application.first\n")
    _write_source(root, "training/domain/values.py", "VALUE = 1\n")

    assert ImportCycles(SourceImportGraph(root, import_time_only=True))


@pytest.mark.parametrize(
    "source",
    (
        "from typing import TYPE_CHECKING as Checking\nif Checking:\n    from . import second\n",
        "import typing as hints\nif hints.TYPE_CHECKING:\n    from . import second\n",
        "from typing import TYPE_CHECKING\nif not TYPE_CHECKING:\n    pass\nelse:\n    from . import second\n",
    ),
)
def test_import_time_graph_excludes_type_only_cycles(tmp_path, source) -> None:
    root = tmp_path / "trader"
    _write_source(root, "training/application/first.py", source)
    _write_source(root, "training/application/second.py", "from . import first\n")

    assert ImportCycles(SourceImportGraph(root, import_time_only=True)) == ()


def test_import_time_graph_excludes_delayed_imports_but_dependency_graph_retains_them(tmp_path) -> None:
    root = tmp_path / "trader"
    _write_source(root, "training/application/first.py", "def execute():\n    from . import second\n")
    _write_source(root, "training/application/second.py", "from . import first\n")

    assert ImportCycles(SourceImportGraph(root, import_time_only=True)) == ()
    assert ImportCycles(SourceImportGraph(root))
