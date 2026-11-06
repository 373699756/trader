"""Business package ownership, dependencies, and composition contracts."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.helpers.shared.module_boundaries import (
    ImportCycles,
    LayerImportViolations,
    ParseSourceImports,
    SourceImportGraph,
    SourceImports,
    SourceImportTargets,
    SourceModuleName,
)
from tests.helpers.shared.python_source import source_text, source_tree
from trader.recommendation.domain.publication.models import Strategy
from trader.training.infra.profile.v2.contracts import V2_TRAINING_PROFILE
from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE


SOURCE_ROOT = Path(__file__).resolve().parents[3] / "src" / "trader"
PROJECT_ROOT = SOURCE_ROOT.parents[1]
TARGET_PACKAGES = (
    "recommendation/domain/market",
    "recommendation/domain/candidate",
    "recommendation/domain/scoring",
    "recommendation/domain/risk",
    "recommendation/domain/evidence",
    "recommendation/domain/selection",
    "recommendation/domain/publication",
    "recommendation/domain/outcomes",
    "recommendation/application/ports",
    "recommendation/application/pipeline",
    "recommendation/application/runtime",
    "recommendation/application/pipeline/freeze_publish",
    "infra/settings",
    "infra/market_data/history",
    "infra/market_data/references",
    "recommendation/infra/market_data",
    "infra/serialization",
    "recommendation/infra/deepseek",
    "download/domain",
    "download/application",
    "download/infra",
    "download/entrypoints",
    "training/application",
    "training/domain",
    "training/domain/evaluation",
    "training/infra",
    "training/infra/research",
    "training/entrypoints",
    "http_api",
    "http_api/routes",
)


def _imports(path: Path) -> set[str]:
    return set(SourceImportTargets(path, SOURCE_ROOT))


def test_product_packages_exist_under_their_business_owners() -> None:
    missing_targets = [target for target in TARGET_PACKAGES if not (SOURCE_ROOT / target).is_dir()]
    assert missing_targets == []
    assert not (PROJECT_ROOT / "docs" / "plan.md").exists()


def test_production_profiles_have_distinct_tomorrow_and_single_d25_heads() -> None:
    assert (V2_TRAINING_PROFILE.algorithm, V3_TRAINING_PROFILE.algorithm) == ("lightgbm", "ridge")
    for profile in (V2_TRAINING_PROFILE, V3_TRAINING_PROFILE):
        assert tuple(head.strategy for head in profile.heads) == (Strategy.TOMORROW, Strategy.D25)
        tomorrow = profile.head_for_strategy(Strategy.TOMORROW)
        d25 = profile.head_for_strategy(Strategy.D25)
        assert tomorrow.model_id != d25.model_id
        assert tomorrow.directory_name != d25.directory_name
        assert tomorrow.target_column == "target_t1"
        assert d25.target_column == "target_d25_aggregate"
        assert d25.label_target == "pre_cost_mean_excess_return_t2_t5"


def test_currently_retired_paths_remain_absent() -> None:
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
    assert [path for path in retired if (SOURCE_ROOT / path).exists()] == []


def test_retired_source_directories_have_no_python_modules() -> None:
    retired_roots = (
        SOURCE_ROOT / "application",
        SOURCE_ROOT / "domain",
        SOURCE_ROOT / "resources",
        SOURCE_ROOT / "web" / "api",
        SOURCE_ROOT / "infra" / "artifacts",
        SOURCE_ROOT / "infra" / "deepseek",
        SOURCE_ROOT / "infra" / "persistence",
        SOURCE_ROOT / "infra" / "pipeline",
        SOURCE_ROOT / "infra" / "research",
        SOURCE_ROOT / "infra" / "scoring",
        SOURCE_ROOT / "recommendation" / "infra" / "scoring" / "profiles",
    )
    source_files = [
        str(file.relative_to(SOURCE_ROOT))
        for path in retired_roots
        if path.exists()
        for file in path.rglob("*.py")
        if "__pycache__" not in file.parts
    ]
    assert source_files == []


def test_shared_infrastructure_target_owners_exist() -> None:
    assert (SOURCE_ROOT / "infra/serialization").is_dir()
    assert (SOURCE_ROOT / "training/infra/profile/composition.py").is_file()


def test_product_import_time_graph_has_no_cycles() -> None:
    assert ImportCycles(SourceImportGraph(SOURCE_ROOT, import_time_only=True)) == ()


def test_market_provider_and_normalization_packages_are_partitioned() -> None:
    market_root = SOURCE_ROOT / "infra" / "market_data"
    provider_root = market_root / "providers"
    recommendation_provider_root = SOURCE_ROOT / "recommendation" / "infra" / "market_data" / "providers"
    normalization_root = SOURCE_ROOT / "recommendation" / "infra" / "normalization"
    assert recommendation_provider_root.is_dir()
    assert normalization_root.is_dir()
    assert (normalization_root / "quote.py").is_file()
    assert {"eastmoney.py", "sina.py", "tencent.py"} <= {
        path.name for path in recommendation_provider_root.glob("*.py")
    }
    assert {"company_disclosure_source.py", "company_structured_source.py"} <= {
        path.name for path in (SOURCE_ROOT / "download" / "infra").glob("*.py")
    }
    assert not (recommendation_provider_root / "akshare.py").exists()
    assert not any(
        (provider_root / name).exists()
        for name in ("akshare.py", "akshare_news.py", "eastmoney.py", "sina.py", "tencent.py")
    )
    assert not any((market_root / "normalization").rglob("*.py"))
    assert not any(
        (market_root / name).exists()
        for name in (
            "akshare.py",
            "cninfo.py",
            "eastmoney.py",
            "exchange_security_master.py",
            "sina.py",
            "tencent.py",
            "tushare.py",
            "tushare_records.py",
            "columnar.py",
            "columnar_merge.py",
            "feature_math.py",
            "feature_risks.py",
            "features.py",
            "field_quality.py",
            "merge.py",
            "merge_quote.py",
            "normalize.py",
        )
    )

    violations: list[str] = []
    for path in normalization_root.rglob("*.py"):
        for imported in _imports(path):
            if imported.startswith("trader.infra.market_data.providers"):
                violations.append(f"{path.relative_to(SOURCE_ROOT)} -> {imported}")
    assert violations == []


def test_business_and_delivery_boundaries_do_not_import_provider_implementations() -> None:
    roots = (
        SOURCE_ROOT / "recommendation" / "application",
        SOURCE_ROOT / "recommendation" / "domain",
        SOURCE_ROOT / "download",
        SOURCE_ROOT / "training",
        SOURCE_ROOT / "http_api",
        SOURCE_ROOT / "web",
    )
    violations: list[str] = []
    for root in roots:
        for path in root.rglob("*.py"):
            imports = _imports(path)
            tree = source_tree(path)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    imports.update(f"{node.module}.{alias.name}" for alias in node.names)
            for imported in sorted(imports):
                if imported.startswith("trader.infra.market_data.providers"):
                    violations.append(f"{path.relative_to(SOURCE_ROOT)} -> {imported}")
    assert violations == []


def test_market_history_references_and_services_are_partitioned() -> None:
    market_root = SOURCE_ROOT / "infra" / "market_data"
    history_root = market_root / "history"
    references_root = market_root / "references"
    service_root = SOURCE_ROOT / "recommendation" / "infra" / "market_data"
    assert history_root.is_dir()
    assert references_root.is_dir()
    assert service_root.is_dir()
    assert (service_root / "market_feature_service.py").is_file()
    assert (service_root / "published_history_cache.py").is_file()
    legacy_files = (
        "daily_history_cache.py",
        "daily_history_warmup.py",
        "gateway.py",
        "gateway_health.py",
        "gateway_runtime.py",
        "market_cache_identity.py",
        "service.py",
        "trading_calendar_state_codec.py",
        "candidate_quote_cache.py",
        "market_task_runner.py",
        "market_data_health.py",
        "intraday_loader.py",
        "market_feature_cache_entries.py",
        "research_observation_loader.py",
        "research_component_persistence.py",
        "research_load_status.py",
        "tushare_reference_loader.py",
        "source_coordinator.py",
    )
    assert not any((market_root / name).exists() for name in legacy_files)


def test_recommendation_stages_are_partitioned_without_reverse_dependencies() -> None:
    recommendation_root = SOURCE_ROOT / "recommendation" / "domain"
    candidate_root = recommendation_root / "candidate"
    scoring_root = recommendation_root / "scoring"
    risk_root = recommendation_root / "risk"
    selection_root = recommendation_root / "selection"
    assert candidate_root.is_dir()
    assert scoring_root.is_dir()
    assert risk_root.is_dir()
    assert selection_root.is_dir()
    legacy_files = (
        "filters.py",
        "scoring.py",
        "scoring_calculations.py",
        "downside.py",
        "fusion.py",
        "scored_fusion.py",
        "ranking.py",
        "scored_selection.py",
    )
    assert not any((recommendation_root / name).exists() for name in legacy_files)

    stage_roots = {
        "candidate": candidate_root,
        "scoring": scoring_root,
        "risk": risk_root,
        "selection": selection_root,
    }
    stage_order = {name: index for index, name in enumerate(stage_roots)}
    violations: list[str] = []
    for stage, root in stage_roots.items():
        for path in root.rglob("*.py"):
            for imported in _imports(path):
                prefix = "trader.recommendation.domain."
                if not imported.startswith(prefix):
                    continue
                imported_stage = next((name for name in stage_roots if imported[len(prefix) :].startswith(name)), None)
                if imported_stage is not None and stage_order[imported_stage] > stage_order[stage]:
                    violations.append(f"{path.relative_to(SOURCE_ROOT)} -> {imported}")
    assert violations == []


def test_recommendation_scoring_and_publication_are_owned_by_pipeline_stages() -> None:
    application_root = SOURCE_ROOT / "application"
    pipeline_root = SOURCE_ROOT / "recommendation/application/pipeline"
    expected = {
        "local_score": {"base_scoring.py", "model_scoring.py", "model_router.py"},
        "risk_review": {"deepseek_evidence_gate.py"},
        "score_merge": {"score_fusion.py"},
        "downside_action": {"downside_protection.py"},
        "final_selection": {"freeze_projection.py", "grouped_ranking.py"},
        "freeze_publish": {
            "snapshot_publisher.py",
            "freeze_coordinator.py",
            "runtime_adapters.py",
            "read_only_queries.py",
            "event_stream.py",
        },
    }
    for stage, files in expected.items():
        root = pipeline_root / stage
        assert root.is_dir()
        assert files <= {path.name for path in root.glob("*.py")}

    assert not any(
        path.is_file() for name in ("recommendation", "freezes") for path in (application_root / name).rglob("*.py")
    )
    retired_files = {
        "scored_projection.py",
        "scored_freezing.py",
        "production_model_scoring.py",
        "freeze_core.py",
        "freeze_coverage.py",
        "freeze_drafts.py",
        "freeze_events.py",
        "freeze_observers.py",
        "freeze_overlay_refresh.py",
        "freeze_queries.py",
        "freeze_stream.py",
        "freeze_adapters.py",
    }
    assert not any(path.name in retired_files for path in application_root.rglob("*.py"))


def test_application_runtime_and_market_data_are_partitioned() -> None:
    application_root = SOURCE_ROOT / "recommendation" / "application"
    runtime_root = application_root / "runtime"
    assert runtime_root.is_dir()
    assert not (application_root / "market_data").exists()

    runtime_files = {
        "cadence.py",
        "latency.py",
        "supervisor.py",
        "schedule.py",
        "source_lanes.py",
        "resource_orchestration.py",
        "latest_wins.py",
        "scheduler_runtime.py",
        "runtime_issues.py",
    }
    assert {path.name for path in runtime_root.glob("*.py")} >= runtime_files
    assert (SOURCE_ROOT / "recommendation/application/pipeline/data_source/source_router.py").is_file()
    assert not any((SOURCE_ROOT / "application" / name).exists() for name in runtime_files | {"input_runtime.py"})

    violations: list[str] = []
    for path in runtime_root.rglob("*.py"):
        for imported in _imports(path):
            if imported.startswith("trader.recommendation.application.pipeline") and not imported.startswith(
                "trader.recommendation.application.pipeline.freeze_publish"
            ):
                violations.append(f"{path.relative_to(SOURCE_ROOT)} -> {imported}")
    assert violations == []


def test_http_api_and_presentation_resources_are_partitioned() -> None:
    web_root = SOURCE_ROOT / "web"
    api_root = SOURCE_ROOT / "http_api"
    api_files = {"route_services.py"}

    assert api_root.is_dir()
    assert {path.name for path in api_root.glob("*.py")} >= api_files
    assert (api_root / "response" / "freeze_projection.py").is_file()
    assert (api_root / "routes" / "page_routes.py").is_file()
    assert not any((web_root / "api").glob("*.py"))
    assert not any((web_root / name).exists() for name in api_files | {"routes_v2.py"})
    assert (web_root / "app.py").is_file()
    assert (web_root / "static_assets.py").is_file()
    assert (web_root / "templates").is_dir()
    assert (web_root / "static").is_dir()

    forbidden_imports = ("trader.infra", "trader.bootstrap", "trader.entrypoints")
    violations = [
        f"{path.relative_to(SOURCE_ROOT)} -> {imported}"
        for path in api_root.rglob("*.py")
        for imported in _imports(path)
        if imported.startswith(forbidden_imports)
    ]
    assert violations == []


def test_application_research_and_outcome_services_are_partitioned() -> None:
    application_root = SOURCE_ROOT / "training/application"
    research_root = application_root
    outcomes_root = SOURCE_ROOT / "recommendation/application"
    research_files = {
        "research_audit.py",
        "research_runtime.py",
    }

    assert {path.name for path in research_root.glob("*.py")} >= research_files
    assert (outcomes_root / "outcome_settlement.py").is_file()
    assert (outcomes_root / "ports/outcomes.py").is_file()
    assert not (research_root / "outcome_settlement.py").exists()
    assert not (research_root / "outcome_ports.py").exists()
    assert not any(
        (research_root / name).exists()
        for name in (
            "tomorrow_profile_comparison.py",
            "tomorrow_profile_reporting.py",
            "tomorrow_profile_settlement.py",
            "profile_evidence_ports.py",
        )
    )

    violations = [
        f"{path.relative_to(SOURCE_ROOT)} -> {imported}"
        for root in (application_root, outcomes_root)
        for path in root.rglob("*.py")
        for imported in LayerImportViolations(SourceModuleName(path, SOURCE_ROOT), SourceImports(path, SOURCE_ROOT))
    ]
    assert violations == []


def test_recommendation_runtime_uses_ports_instead_of_training_entrypoints() -> None:
    input_runtime = source_text(SOURCE_ROOT / "recommendation/application/pipeline/data_source/source_router.py")
    freeze_adapters = source_text(SOURCE_ROOT / "recommendation/application/pipeline/freeze_publish/runtime_adapters.py")
    local_builder = source_text(SOURCE_ROOT / "recommendation/application/pipeline/local_score/local_freeze_builder.py")

    assert "CandidateFilteringPort" in input_runtime
    assert "LocalScoringPort" not in input_runtime
    assert "LocalScoringPort" in local_builder
    assert "ScoreFusionPort" in freeze_adapters
    assert "build_candidate_plans" not in input_runtime
    assert "refresh_candidate_reserves" not in input_runtime
    assert "build_scored_local" not in input_runtime
    assert "build_scored_hybrid" not in freeze_adapters
    assert "run_v2_training" not in input_runtime
    assert "run_v3_training" not in input_runtime
    assert "run_v2_training" not in local_builder
    assert "run_v3_training" not in local_builder


def test_composition_root_explicitly_imports_recommendation_capabilities() -> None:
    bootstrap = SOURCE_ROOT / "bootstrap.py"
    imported = {item.member for item in SourceImports(bootstrap, SOURCE_ROOT)}
    for capability in (
        "CandidateFilteringService",
        "LocalScoringService",
        "RankingSelectionService",
        "RiskControlService",
        "ScoreFusionService",
    ):
        assert capability in imported

    factory = SOURCE_ROOT / "recommendation/infra/scoring/profile_factory.py"
    assert "trader.recommendation.infra.scoring.profile_factory" in SourceImportTargets(bootstrap, SOURCE_ROOT)
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "profile_factory"
        and node.func.attr == "BuildProfileScoring"
        for node in ast.walk(source_tree(bootstrap))
    )
    assert "PublishedModelScoringService" in {item.member for item in SourceImports(factory, SOURCE_ROOT)}


def test_infrastructure_technical_capabilities_have_single_owners() -> None:
    atomic = SOURCE_ROOT / "infra" / "atomic_files" / "json.py"
    clock = SOURCE_ROOT / "infra" / "clock" / "shanghai.py"
    assert atomic.is_file()
    assert clock.is_file()
    assert not (SOURCE_ROOT / "infra" / "persistence" / "runtime_json.py").exists()
    assert "class RuntimeJsonWriter" in source_text(atomic)
    assert "class ShanghaiClock" in source_text(clock)

    definitions: dict[str, list[Path]] = {
        "RuntimeJsonWriter": [],
        "AtomicReadJson": [],
        "AtomicWriteJson": [],
        "ShanghaiClock": [],
    }
    for path in SOURCE_ROOT.rglob("*.py"):
        tree = source_tree(path)
        for node in tree.body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in definitions:
                definitions[node.name].append(path.relative_to(SOURCE_ROOT))

    assert definitions == {
        "RuntimeJsonWriter": [Path("infra/atomic_files/json.py")],
        "AtomicReadJson": [Path("infra/atomic_files/json.py")],
        "AtomicWriteJson": [Path("infra/atomic_files/json.py")],
        "ShanghaiClock": [Path("infra/clock/shanghai.py")],
    }


def test_recommendation_pipeline_capabilities_have_implementation_owners() -> None:
    pipeline = SOURCE_ROOT / "recommendation/application/pipeline"
    for name in (
        "static_filter",
        "dynamic_filter",
        "candidate_pool",
        "quality_check",
        "local_score",
        "risk_review",
        "score_merge",
        "final_selection",
    ):
        assert any(path.name != "__init__.py" for path in (pipeline / name).rglob("*.py")), name


DOWNLOAD_APPLICATION = PROJECT_ROOT / "src" / "trader" / "download" / "application"


TRAINING_APPLICATION = PROJECT_ROOT / "src" / "trader" / "training" / "application"


def test_download_business_does_not_import_training_implementations_or_values() -> None:
    source_root = PROJECT_ROOT / "src/trader"
    imports = [
        f"{path.relative_to(source_root)}:{imported.line} -> {imported.module}"
        for path in (source_root / "download").rglob("*.py")
        for imported in SourceImports(path, source_root)
        if imported.module == "trader.training" or imported.module.startswith("trader.training.")
    ]
    assert imports == []


@pytest.mark.parametrize("business", ("download", "training"))
@pytest.mark.parametrize(
    ("source", "rejected"),
    (
        ("from trader.infra.workers import WorkerExecutor, ManagedWorkerExecutor, SubmitOrReject", False),
        ("from trader.infra.workers import SubmitWithBackpressure, WorkerResourceRejectedError", False),
        ("from trader.infra.shutdown import ShutdownDeadline, ShutdownStep", False),
        ("from trader.infra.workers import BoundedExecutor", True),
        ("from trader.infra.workers import *", True),
        ("import trader.infra.workers as workers", True),
        ("from trader.infra import workers", True),
        ("from trader.infra.shutdown import ShutdownSignalController", True),
        ("from trader.BUSINESS.infra import adapter", True),
        ("from trader.BUSINESS import infra", True),
        ("from trader.recommendation.infra import adapter", True),
        ("from ..infra import adapter", True),
        ("from .. import infra", True),
        ("from trader.BUSINESS.entrypoints import commands", True),
        ("from trader.training.entrypoints import commands", True),
        ("from trader.download.entrypoints import commands", True),
        ("from trader.http_api import app", True),
        ("from flask import Flask", True),
        ("import flask", True),
    ),
)
def test_application_guard_accepts_only_narrow_shared_capabilities(business, source, rejected) -> None:
    module = f"trader.{business}.application.example"
    assert (
        bool(LayerImportViolations(module, ParseSourceImports(source.replace("BUSINESS", business), module)))
        is rejected
    )


def test_history_application_package_exposes_split_use_cases_and_ports() -> None:
    package = DOWNLOAD_APPLICATION
    assert {
        "download_history.py",
        "history_ports.py",
    } <= {path.name for path in package.glob("*.py")}
    violations = [
        f"{path.relative_to(package)}: {imported}"
        for path in package.rglob("*.py")
        for imported in LayerImportViolations(
            SourceModuleName(path, PROJECT_ROOT / "src/trader"),
            SourceImports(path, PROJECT_ROOT / "src/trader"),
        )
    ]
    assert violations == []


def test_training_application_package_is_profile_owned_and_infrastructure_free() -> None:
    package = TRAINING_APPLICATION
    assert {
        "profile_training_primary.py",
        "profile_training_secondary.py",
        "training_due.py",
        "training_ports.py",
    } <= {path.name for path in package.glob("*.py")}
    violations = [
        f"{path.relative_to(package)}: {imported}"
        for path in package.rglob("*.py")
        for imported in LayerImportViolations(
            SourceModuleName(path, PROJECT_ROOT / "src/trader"),
            SourceImports(path, PROJECT_ROOT / "src/trader"),
        )
    ]
    assert violations == []


def test_entrypoints_route_download_and_joint_training_through_current_owners() -> None:
    commands = (PROJECT_ROOT / "src" / "trader" / "training" / "entrypoints" / "commands.py").read_text(encoding="utf-8")
    download_commands = (PROJECT_ROOT / "src" / "trader" / "download" / "entrypoints" / "commands.py").read_text(
        encoding="utf-8"
    )
    assert "ExecuteHistoryDownload" in download_commands
    assert "DownloadHistoryUseCase" in (PROJECT_ROOT / "src/trader/bootstrap.py").read_text(encoding="utf-8")
    selected = (PROJECT_ROOT / "src/trader/training/infra/selected_training.py").read_text(encoding="utf-8")
    assert "RunSelectedTrainingTasks" in selected
    assert "InspectSelectedProfile" in selected
    assert "TrainV2UseCase" not in commands
    assert "TrainV3UseCase" not in commands
