from __future__ import annotations

import ast
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "trader"
PROJECT_ROOT = SOURCE_ROOT.parents[1]
DESIGN = PROJECT_ROOT / "docs" / "02_工程设计.md"

TARGET_PACKAGES = (
    "recommendation/domain/market",
    "recommendation/domain/candidate",
    "recommendation/domain/scoring",
    "recommendation/domain/risk",
    "recommendation/domain/evidence",
    "recommendation/domain/selection",
    "recommendation/domain/publication",
    "recommendation/application/ports",
    "recommendation/application/pipeline",
    "recommendation/application/runtime",
    "recommendation/application/pipeline/freeze_publish",
    "infra/settings",
    "infra/market_data/providers",
    "infra/market_data/normalization",
    "infra/market_data/history",
    "infra/market_data/references",
    "infra/market_data/service",
    "recommendation/infra/deepseek",
    "infra/persistence",
    "download/domain",
    "download/application",
    "download/infra",
    "download/entrypoints",
    "training/application",
    "training/domain",
    "training/evaluation/domain",
    "training/evaluation/application",
    "training/infra",
    "training/infra/research",
    "training/entrypoints",
    "http_api",
    "http_api/routes",
)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def test_final_target_packages_are_documented() -> None:
    design = DESIGN.read_text(encoding="utf-8")
    missing_targets = [target for target in TARGET_PACKAGES if target not in design]
    assert missing_targets == []
    assert not (PROJECT_ROOT / "docs" / "plan.md").exists()


def test_scoring_profile_capability_matrix_uses_one_quality_scale_and_d25_single_head() -> None:
    design = DESIGN.read_text(encoding="utf-8")
    strategy = (PROJECT_ROOT / "docs" / "01_评分逻辑.md").read_text(encoding="utf-8")

    for token in (
        "| 评分档位 | Tomorrow | D25 |",
        "| V2 | 板块证据质量评分 + V2 Tomorrow 预测头 | 板块证据质量评分 + V2 D25 预测头 |",
        "| V3 | 板块证据质量评分 + V3 Tomorrow 预测头 | 板块证据质量评分 + V3 D25 预测头 |",
        "D25 生产边界只输出一个面向未来 2–5 个交易日的策略信号",
        "不得拆成 T+2、T+3、T+4、T+5 四个生产头",
    ):
        assert token in design
    assert "D25 是一个完整且唯一的 2–5 日生产策略头" in strategy
    assert "四个观察点只用于 D25 的标签、终端留出、风险和稳定性评价" in strategy
    assert "不覆盖公共评分器产生的 `base_score`" in design
    assert "profile 不再复制单一" in design
    assert "model ID/hash" in design


def test_currently_retired_paths_remain_absent() -> None:
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
    assert [path for path in retired if (SOURCE_ROOT / path).exists()] == []


def test_layer_import_graph_has_no_cycles_or_reverse_edges() -> None:
    layers = ("domain", "application", "infra", "web", "entrypoints")
    forbidden = {
        "domain": {"application", "infra", "web", "entrypoints"},
        "application": {"infra", "web", "entrypoints"},
        "infra": {"web", "entrypoints"},
        "web": {"infra", "entrypoints"},
        "entrypoints": set(),
    }
    graph: dict[str, set[str]] = {layer: set() for layer in layers}
    violations: list[str] = []
    for layer in layers:
        for path in (SOURCE_ROOT / layer).rglob("*.py"):
            for imported in _imports(path):
                prefix = imported.removeprefix("trader.").split(".", 1)[0]
                if prefix not in graph or prefix == layer:
                    continue
                graph[layer].add(prefix)
                if prefix in forbidden[layer]:
                    violations.append(f"{path.relative_to(SOURCE_ROOT)} -> trader.{prefix}")

    def visit(node: str, stack: tuple[str, ...] = ()) -> None:
        if node in stack:
            violations.append("cycle: " + " -> ".join((*stack, node)))
            return
        for child in graph[node]:
            visit(child, (*stack, node))

    for layer in layers:
        visit(layer)
    assert violations == []


def test_recommendation_application_has_no_reverse_or_cross_business_dependencies() -> None:
    root = SOURCE_ROOT / "recommendation/application"
    forbidden = ("trader.infra", "trader.web", "trader.entrypoints", "trader.training")
    violations = [
        f"{path.relative_to(SOURCE_ROOT)} -> {imported}"
        for path in root.rglob("*.py")
        for imported in _imports(path)
        if imported.startswith(forbidden)
    ]
    assert violations == []


def test_market_provider_and_normalization_packages_are_partitioned() -> None:
    market_root = SOURCE_ROOT / "infra" / "market_data"
    provider_root = market_root / "providers"
    normalization_root = market_root / "normalization"
    assert provider_root.is_dir()
    assert normalization_root.is_dir()
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


def test_market_history_references_and_services_are_partitioned() -> None:
    market_root = SOURCE_ROOT / "infra" / "market_data"
    history_root = market_root / "history"
    references_root = market_root / "references"
    service_root = market_root / "service"
    assert history_root.is_dir()
    assert references_root.is_dir()
    assert service_root.is_dir()
    assert (service_root / "market_feature_service.py").is_file()
    legacy_files = (
        "history.py",
        "history_seed.py",
        "daily_history_cache.py",
        "daily_history_warmup.py",
        "calendar.py",
        "security_references.py",
        "gateway.py",
        "gateway_health.py",
        "gateway_runtime.py",
        "market_cache_identity.py",
        "observations.py",
        "router.py",
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
        "final_selection": {"decision_projection.py", "grouped_ranking.py"},
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
        path.is_file()
        for name in ("recommendation", "decisions")
        for path in (application_root / name).rglob("*.py")
    )
    retired_files = {
        "scored_projection.py",
        "scored_freezing.py",
        "production_model_scoring.py",
        "decision_core.py",
        "decision_coverage.py",
        "decision_drafts.py",
        "decision_events.py",
        "decision_observers.py",
        "decision_overlay_refresh.py",
        "decision_queries.py",
        "decision_stream.py",
        "decision_adapters.py",
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
        "shutdown.py",
        "source_lanes.py",
        "resource_orchestration.py",
        "latest_wins.py",
        "scheduler_runtime.py",
        "runtime_issues.py",
        "workers.py",
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
    api_files = {
        "decision_serializers.py",
        "decision_sse.py",
        "route_services.py",
    }

    assert api_root.is_dir()
    assert {path.name for path in api_root.glob("*.py")} >= api_files
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
    application_root = SOURCE_ROOT / "training/evaluation/application"
    research_root = application_root
    outcomes_root = application_root
    research_files = {
        "research_audit.py",
        "research_coordination.py",
        "research_runtime.py",
    }

    assert {path.name for path in research_root.glob("*.py")} >= research_files
    assert (outcomes_root / "outcome_settlement.py").is_file()
    assert (outcomes_root / "outcome_ports.py").is_file()
    assert not any(
        (research_root / name).exists()
        for name in (
            "tomorrow_profile_comparison.py",
            "tomorrow_profile_reporting.py",
            "tomorrow_profile_settlement.py",
            "profile_evidence_ports.py",
        )
    )

    forbidden_imports = ("trader.infra", "trader.web", "trader.entrypoints")
    violations = [
        f"{path.relative_to(SOURCE_ROOT)} -> {imported}"
        for package_root in (research_root, outcomes_root)
        for path in package_root.rglob("*.py")
        for imported in _imports(path)
        if imported.startswith(forbidden_imports)
    ]
    assert violations == []
