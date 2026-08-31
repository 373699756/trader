from __future__ import annotations

import ast
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "trader"
PROJECT_ROOT = SOURCE_ROOT.parents[1]
DESIGN = PROJECT_ROOT / "docs" / "software-business-design.md"
PLAN = PROJECT_ROOT / "docs" / "plan.md"

# This is the migration inventory. Keep entries explicit so a new module cannot
# silently join a package migration without a contract/test review.
MIGRATION_LEDGER: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        "batch-2",
        (
            "infra/settings.py",
            "infra/settings_credentials.py",
            "infra/settings_factor_validation.py",
            "infra/settings_market_policy.py",
            "infra/settings_models.py",
            "infra/settings_parser.py",
            "infra/settings_runtime.py",
            "infra/settings_strategy_validation.py",
        ),
        "infra/settings",
    ),
    (
        "batch-3",
        (
            "infra/market_data/akshare.py",
            "infra/market_data/akshare_news.py",
            "infra/market_data/akshare_parsing.py",
            "infra/market_data/cninfo.py",
            "infra/market_data/eastmoney.py",
            "infra/market_data/exchange_security_master.py",
            "infra/market_data/sina.py",
            "infra/market_data/tencent.py",
            "infra/market_data/tushare.py",
            "infra/market_data/tushare_records.py",
        ),
        "infra/market_data/providers",
    ),
    (
        "batch-3",
        (
            "infra/market_data/columnar.py",
            "infra/market_data/columnar_merge.py",
            "infra/market_data/feature_math.py",
            "infra/market_data/feature_risks.py",
            "infra/market_data/features.py",
            "infra/market_data/field_quality.py",
            "infra/market_data/merge.py",
            "infra/market_data/merge_quote.py",
            "infra/market_data/normalize.py",
        ),
        "infra/market_data/normalization",
    ),
    (
        "batch-4",
        (
            "infra/market_data/history.py",
            "infra/market_data/history_seed.py",
            "infra/market_data/service_history.py",
            "infra/market_data/service_history_warmup.py",
        ),
        "infra/market_data/history",
    ),
    (
        "batch-4",
        (
            "infra/market_data/calendar.py",
            "infra/market_data/security_references.py",
        ),
        "infra/market_data/references",
    ),
    (
        "batch-4",
        (
            "infra/market_data/gateway.py",
            "infra/market_data/gateway_health.py",
            "infra/market_data/gateway_runtime.py",
            "infra/market_data/market_cache_identity.py",
            "infra/market_data/observations.py",
            "infra/market_data/router.py",
            "infra/market_data/service.py",
            "infra/market_data/service_calendar_state.py",
            "infra/market_data/service_candidates.py",
            "infra/market_data/service_execution.py",
            "infra/market_data/service_health.py",
            "infra/market_data/service_intraday.py",
            "infra/market_data/service_models.py",
            "infra/market_data/service_research.py",
            "infra/market_data/service_research_data_plane.py",
            "infra/market_data/service_research_models.py",
            "infra/market_data/service_tushare.py",
            "infra/market_data/source_coordinator.py",
        ),
        "infra/market_data/service",
    ),
    (
        "batch-5",
        ("domain/recommendation/filters.py",),
        "domain/recommendation/filtering",
    ),
    (
        "batch-5",
        (
            "domain/recommendation/scoring.py",
            "domain/recommendation/scoring_calculations.py",
        ),
        "domain/recommendation/scoring",
    ),
    (
        "batch-5",
        (
            "domain/recommendation/downside.py",
            "domain/recommendation/fusion.py",
            "domain/recommendation/scored_fusion.py",
        ),
        "domain/recommendation/risk_fusion",
    ),
    (
        "batch-5",
        (
            "domain/recommendation/ranking.py",
            "domain/recommendation/scored_selection.py",
        ),
        "domain/recommendation/selection",
    ),
    (
        "batch-6",
        (
            "application/scored_deepseek_fusion.py",
            "application/scored_quality.py",
            "application/scored_selection.py",
            "application/scored_v2_freezing.py",
            "application/scored_v2_projection.py",
            "application/today_v2_freezing.py",
            "application/tomorrow_model_scoring.py",
            "application/recommendation_policy_codec.py",
            "application/policy.py",
        ),
        "application/recommendation",
    ),
    (
        "batch-6",
        (
            "application/decision_core.py",
            "application/decision_coverage.py",
            "application/decision_drafts.py",
            "application/decision_events.py",
            "application/decision_observers.py",
            "application/decision_overlay_refresh.py",
            "application/decision_queries.py",
            "application/decision_stream.py",
            "application/v2_decision_adapters.py",
        ),
        "application/decisions",
    ),
    (
        "batch-7",
        (
            "application/cadence.py",
            "application/latency.py",
            "application/runtime.py",
            "application/schedule.py",
            "application/shutdown.py",
            "application/source_lanes.py",
            "application/resource_orchestration.py",
            "application/latest_wins.py",
            "application/v2_runtime.py",
            "application/v2_runtime_issues.py",
            "application/workers.py",
        ),
        "application/runtime",
    ),
    (
        "batch-7",
        ("application/v2_input_runtime.py",),
        "application/market_data",
    ),
    (
        "batch-8",
        (
            "web/decision_serializers.py",
            "web/decision_sse.py",
            "web/route_services.py",
            "web/routes.py",
            "web/routes_v2.py",
        ),
        "web/api",
    ),
    (
        "batch-9",
        ("application/outcome_settlement.py",),
        "application/outcomes",
    ),
    (
        "batch-9",
        (
            "application/research_audit.py",
            "application/research_coordination.py",
            "application/tomorrow_profile_comparison.py",
            "application/tomorrow_profile_reporting.py",
            "application/tomorrow_profile_settlement.py",
            "application/v2_research_runtime.py",
        ),
        "application/research",
    ),
)

COMPLETED_BATCHES = frozenset({"batch-2", "batch-3", "batch-4", "batch-5", "batch-6", "batch-7"})

TARGET_PACKAGES = (
    "domain/market",
    "domain/recommendation/filtering",
    "domain/recommendation/scoring",
    "domain/recommendation/risk_fusion",
    "domain/recommendation/selection",
    "domain/research",
    "domain/review",
    "domain/outcome",
    "application/runtime",
    "application/market_data",
    "application/recommendation",
    "application/decisions",
    "application/research",
    "application/outcomes",
    "infra/settings",
    "infra/market_data/providers",
    "infra/market_data/normalization",
    "infra/market_data/history",
    "infra/market_data/references",
    "infra/market_data/service",
    "infra/deepseek",
    "infra/persistence",
    "infra/research",
    "web/api",
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


def test_target_packages_and_migration_ledger_are_documented() -> None:
    design = DESIGN.read_text(encoding="utf-8")
    plan = PLAN.read_text(encoding="utf-8")
    missing_targets = [target for target in TARGET_PACKAGES if target not in design]
    assert missing_targets == []

    ledger_paths = [path for _, paths, _ in MIGRATION_LEDGER for path in paths]
    assert len(ledger_paths) == len(set(ledger_paths))
    for batch, paths, target in MIGRATION_LEDGER:
        if batch in COMPLETED_BATCHES:
            assert [path for path in paths if (SOURCE_ROOT / path).exists()] == []
            assert (SOURCE_ROOT / target).is_dir()
        else:
            assert [path for path in paths if not (SOURCE_ROOT / path).exists()] == []
    documented_text = f"{design}\n{plan}"
    undocumented = [path for path in ledger_paths if Path(path).name not in documented_text]
    assert undocumented == []


def test_migration_ledger_has_one_batch_and_target_owner_per_source() -> None:
    ownership: dict[str, tuple[str, str]] = {}
    for batch, paths, target in MIGRATION_LEDGER:
        assert batch.startswith("batch-")
        assert target in TARGET_PACKAGES
        for path in paths:
            assert path not in ownership
            ownership[path] = (batch, target)
    assert len(ownership) >= 70


def test_currently_retired_paths_remain_absent() -> None:
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
    assert (service_root / "facade.py").is_file()
    legacy_files = (
        "history.py",
        "history_seed.py",
        "service_history.py",
        "service_history_warmup.py",
        "calendar.py",
        "security_references.py",
        "gateway.py",
        "gateway_health.py",
        "gateway_runtime.py",
        "market_cache_identity.py",
        "observations.py",
        "router.py",
        "service.py",
        "service_calendar_state.py",
        "service_candidates.py",
        "service_execution.py",
        "service_health.py",
        "service_intraday.py",
        "service_models.py",
        "service_research.py",
        "service_research_data_plane.py",
        "service_research_models.py",
        "service_tushare.py",
        "source_coordinator.py",
    )
    assert not any((market_root / name).exists() for name in legacy_files)


def test_recommendation_stages_are_partitioned_without_reverse_dependencies() -> None:
    recommendation_root = SOURCE_ROOT / "domain" / "recommendation"
    filtering_root = recommendation_root / "filtering"
    scoring_root = recommendation_root / "scoring"
    risk_root = recommendation_root / "risk_fusion"
    selection_root = recommendation_root / "selection"
    assert filtering_root.is_dir()
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
        "filtering": filtering_root,
        "scoring": scoring_root,
        "risk_fusion": risk_root,
        "selection": selection_root,
    }
    stage_order = {name: index for index, name in enumerate(stage_roots)}
    violations: list[str] = []
    for stage, root in stage_roots.items():
        for path in root.rglob("*.py"):
            for imported in _imports(path):
                prefix = "trader.domain.recommendation."
                if not imported.startswith(prefix):
                    continue
                imported_stage = next((name for name in stage_roots if imported[len(prefix) :].startswith(name)), None)
                if imported_stage is not None and stage_order[imported_stage] > stage_order[stage]:
                    violations.append(f"{path.relative_to(SOURCE_ROOT)} -> {imported}")
    assert violations == []


def test_application_recommendation_and_decisions_are_partitioned() -> None:
    application_root = SOURCE_ROOT / "application"
    recommendation_root = application_root / "recommendation"
    decisions_root = application_root / "decisions"
    assert recommendation_root.is_dir()
    assert decisions_root.is_dir()

    recommendation_files = {
        "scored_selection.py",
        "scored_quality.py",
        "scored_deepseek_fusion.py",
        "scored_v2_projection.py",
        "scored_v2_freezing.py",
        "today_v2_freezing.py",
        "tomorrow_model_scoring.py",
        "recommendation_policy_codec.py",
        "policy.py",
    }
    decision_files = {
        "decision_core.py",
        "decision_coverage.py",
        "decision_drafts.py",
        "decision_events.py",
        "decision_observers.py",
        "decision_overlay_refresh.py",
        "decision_queries.py",
        "decision_stream.py",
        "v2_decision_adapters.py",
    }
    assert {path.name for path in recommendation_root.glob("*.py")} >= recommendation_files
    assert {path.name for path in decisions_root.glob("*.py")} >= decision_files
    assert not any((application_root / name).exists() for name in recommendation_files | decision_files)

    violations: list[str] = []
    for path in recommendation_root.rglob("*.py"):
        for imported in _imports(path):
            if imported.startswith("trader.application.decisions"):
                violations.append(f"{path.relative_to(SOURCE_ROOT)} -> {imported}")
    assert violations == []


def test_application_runtime_and_market_data_are_partitioned() -> None:
    application_root = SOURCE_ROOT / "application"
    runtime_root = application_root / "runtime"
    market_data_root = application_root / "market_data"
    assert runtime_root.is_dir()
    assert market_data_root.is_dir()

    runtime_files = {
        "cadence.py",
        "latency.py",
        "runtime.py",
        "schedule.py",
        "shutdown.py",
        "source_lanes.py",
        "resource_orchestration.py",
        "latest_wins.py",
        "v2_runtime.py",
        "v2_runtime_issues.py",
        "workers.py",
    }
    assert {path.name for path in runtime_root.glob("*.py")} >= runtime_files
    assert (market_data_root / "v2_input_runtime.py").is_file()
    assert not any((application_root / name).exists() for name in runtime_files | {"v2_input_runtime.py"})

    violations: list[str] = []
    for path in runtime_root.rglob("*.py"):
        for imported in _imports(path):
            if imported.startswith("trader.application.market_data"):
                violations.append(f"{path.relative_to(SOURCE_ROOT)} -> {imported}")
    assert violations == []
