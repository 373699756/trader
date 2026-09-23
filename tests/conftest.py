from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from trader.recommendation.application.pipeline.policy import RecommendationPolicy, RecommendationSelectionSettings
from trader.recommendation.domain.evidence.review import RiskRule
from trader.recommendation.domain.market.models import (
    FeatureSnapshot,
    MarketQuote,
)
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.risk.fusion import DIMENSION_NAMES, FusionPolicy

_TEST_DIRECTORY_MARKERS = frozenset({"unit", "component", "integration", "contract", "performance", "js"})
_BUSINESS_OWNERS = frozenset({"train", "history", "recommendation", "crosscut"})
_TRAINING_COMPONENT_PATHS = frozenset(
    {
        "component/test_factor_diagnostic_reports.py",
        "component/test_historical_baseline_reports.py",
        "component/test_historical_label_artifacts.py",
        "component/test_historical_partitions.py",
        "component/test_research_trace_archive.py",
        "component/test_terminal_holdout_artifacts.py",
        "component/test_tomorrow_historical_artifact_archive.py",
        "component/test_tomorrow_historical_risk_artifacts.py",
    }
)
_HISTORY_PATHS = frozenset(
    {
        "contract/test_baostock_history_cli.py",
        "contract/test_history_archive_repack_contract.py",
        "contract/test_history_automation_contract.py",
        "unit/application/research/test_history_automation.py",
        "unit/application/research/test_history_sync.py",
        "unit/domain/research/test_history_control.py",
        "unit/domain/research/test_history_revision.py",
        "unit/entrypoints/test_history_sync_progress.py",
        "unit/infra/research/test_baostock_gap_supplier.py",
        "unit/infra/research/test_baostock_gateway.py",
        "unit/infra/research/test_baostock_sync_supplier.py",
        "unit/infra/research/test_history_archive_reader.py",
        "unit/infra/research/test_history_archive_repack.py",
        "unit/infra/research/test_history_archive_status.py",
        "unit/infra/research/test_history_archive_sync.py",
        "unit/infra/research/test_history_automation_installation.py",
        "unit/infra/research/test_history_control_repository.py",
        "unit/infra/research/test_history_maintenance_runner.py",
        "unit/infra/research/test_history_month_partition.py",
        "unit/infra/test_baostock_industry.py",
        "unit/scripts/test_convert_baostock_history.py",
        "unit/scripts/test_history_daily_capability.py",
        "unit/scripts/test_history_sources.py",
        "unit/scripts/test_tushare_daily.py",
    }
)
_TRAINING_CONTRACT_PATHS = frozenset(
    {
        "contract/test_baseline_identity_audit_contract.py",
        "contract/test_candidate_recall_ledger_contract.py",
        "contract/test_historical_industry_facts_contract.py",
        "contract/test_historical_only_score_validation.py",
        "contract/test_limited_factor_family_research_contract.py",
        "contract/test_point_in_time_dataset_contract.py",
        "contract/test_risk_cost_uncertainty_contract.py",
        "contract/test_score_cost_aware_selection_contract.py",
        "contract/test_score_plan_contract.py",
        "contract/test_score_point_in_time_population_contract.py",
        "contract/test_score_research_detailed_strategy_contract.py",
        "contract/test_score_shadow_model_contract.py",
        "contract/test_score_tomorrow_features_contract.py",
        "contract/test_scoring_hot_path_efficiency_contract.py",
        "contract/test_scoring_weight_configuration_contract.py",
    }
)
_RECOMMENDATION_CONTRACT_PATHS = frozenset(
    {
        "contract/test_candidate_planning_contract.py",
        "contract/test_current_product_contract.py",
        "contract/test_d25_contract.py",
        "contract/test_decision_contract.py",
        "contract/test_feature_contract_unification.py",
        "contract/test_long_contract.py",
        "contract/test_realtime_pipeline_contract.py",
        "contract/test_recommendation_sections.py",
        "contract/test_runtime_contract.py",
        "contract/test_source_capability.py",
        "contract/test_tomorrow_contract.py",
        "contract/test_two_level_hard_filter_contract.py",
        "contract/test_web_contract.py",
    }
)
_TRAINING_SCRIPT_PATHS = frozenset(
    {
        "unit/scripts/test_audit_historical_industry_facts.py",
        "unit/scripts/test_check_tomorrow_training_memory.py",
        "unit/scripts/test_h1_capability_execution.py",
        "unit/scripts/test_point_in_time_terminal_holdout.py",
        "unit/scripts/test_qualify_point_in_time_data.py",
    }
)
_TRAINING_ENTRYPOINT_PATHS = frozenset(
    {
        "unit/entrypoints/test_h1_point_in_time.py",
        "unit/entrypoints/test_tomorrow_training_progress.py",
    }
)
_RECOMMENDATION_INFRA_PATHS = frozenset(
    {
        "unit/infra/test_cninfo_incremental.py",
        "unit/infra/test_data_plane.py",
        "unit/infra/test_decision_records.py",
        "unit/infra/test_issuer_eligibility_registry.py",
    }
)
_RECOMMENDATION_SCRIPT_PATHS = frozenset(
    {
        "unit/scripts/test_desktop_dashboard.py",
        "unit/scripts/test_diagnose_runtime.py",
        "unit/scripts/test_migrate_runtime_data.py",
        "unit/scripts/test_runtime_diagnostics_reporting.py",
        "unit/scripts/test_security_master_probe.py",
        "unit/scripts/test_web_health.py",
    }
)
_CROSSCUT_PATHS = frozenset(
    {
        "component/test_process_lock.py",
        "unit/application/test_history_training_boundaries.py",
        "unit/infra/test_artifact_publication.py",
        "unit/infra/test_exchange_security_master.py",
        "unit/test_infra_failures.py",
        "unit/test_server_entrypoint.py",
        "unit/test_settings.py",
    }
)
_SLOW_MARKERS_BY_PATH = {
    "slow_history": frozenset(
        {
            "unit/infra/research/test_history_archive_sync.py",
            "unit/infra/research/test_history_archive_repack.py",
            "unit/infra/research/test_history_control_repository.py",
            "unit/infra/research/test_history_training_input.py",
        }
    ),
    "slow_migration": frozenset(
        {
            "unit/infra/research/test_history_archive_sync.py",
            "unit/infra/research/test_history_archive_repack.py",
            "unit/infra/research/test_history_control_repository.py",
            "unit/infra/research/test_history_training_input.py",
            "unit/scripts/test_convert_baostock_history.py",
        }
    ),
    "slow_runtime": frozenset(
        {
            "integration/test_scheduler_runtime.py",
            "unit/application/test_input_runtime.py",
            "unit/application/test_workers.py",
            "unit/application/test_supervisor.py",
        }
    ),
    "slow_supplier": frozenset(
        {
            "component/test_market_vendors.py",
            "component/test_market_gateway.py",
            "component/test_market_service.py",
            "component/test_market_references.py",
            "component/test_market_exchange_references.py",
            "component/test_market_research.py",
            "unit/test_market_data_cache.py",
        }
    ),
}


def _business_owner(relative_path: str) -> str:
    if relative_path in _CROSSCUT_PATHS:
        return "crosscut"
    if relative_path in _HISTORY_PATHS:
        return "history"
    if relative_path in (
        _TRAINING_COMPONENT_PATHS | _TRAINING_CONTRACT_PATHS | _TRAINING_SCRIPT_PATHS | _TRAINING_ENTRYPOINT_PATHS
    ):
        return "train"
    if relative_path in _RECOMMENDATION_CONTRACT_PATHS | _RECOMMENDATION_INFRA_PATHS | _RECOMMENDATION_SCRIPT_PATHS:
        return "recommendation"
    if relative_path.startswith("unit/application/outcomes/"):
        return "train"
    if relative_path.startswith("unit/application/research/"):
        return "train"
    if relative_path.startswith("unit/domain/research/"):
        return "train"
    if relative_path.startswith("unit/infra/research/"):
        return "train"
    if relative_path.startswith("unit/infra/scoring/"):
        return "train"
    if relative_path.startswith("unit/training/"):
        return "train"
    if relative_path == "unit/domain/test_outcomes.py":
        return "train"
    if relative_path == "unit/infra/test_historical_screening_archive.py":
        return "train"
    if relative_path == "unit/infra/test_outcomes.py":
        return "train"
    if relative_path.startswith("component/"):
        return "recommendation"
    if relative_path.startswith("integration/"):
        return "recommendation"
    if relative_path.startswith("performance/"):
        return "recommendation"
    if relative_path.startswith("unit/application/"):
        return "recommendation"
    if relative_path.startswith("unit/domain/"):
        return "recommendation"
    if relative_path.startswith("unit/"):
        return "recommendation"
    if relative_path.startswith("contract/"):
        return "crosscut"
    raise pytest.UsageError(f"Test has no registered business owner: tests/{relative_path}")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        relative = item.path.relative_to(Path(__file__).parent)
        category = relative.parts[0]
        if category in _TEST_DIRECTORY_MARKERS:
            item.add_marker(category)
        business_owner = _business_owner(relative.as_posix())
        if business_owner not in _BUSINESS_OWNERS:
            raise pytest.UsageError(f"Invalid business owner {business_owner!r} for {relative}")
        item.add_marker(business_owner)
        if category == "performance":
            item.add_marker("slow")
        for marker, paths in _SLOW_MARKERS_BY_PATH.items():
            if relative.as_posix() in paths:
                item.add_marker(marker)
                item.add_marker("slow")


@pytest.fixture
def recommendation_policy() -> RecommendationPolicy:
    risk_rules = {
        "near_limit_crowding": RiskRule("near_limit_crowding", "medium", 5.0, 0.7, "market_crowding"),
        "price_volume_divergence": RiskRule("price_volume_divergence", "medium", 4.0, 0.7, "market_structure"),
        "high_volatility": RiskRule("high_volatility", "low", 3.0, 0.7, "market_structure"),
        "financial_deterioration": RiskRule("financial_deterioration", "high", 6.0, 0.7, "financial"),
    }
    return RecommendationPolicy(
        strategy_version="strategy-current",
        fusion_version="fusion-fixture",
        fusion=FusionPolicy(0.68, 0.32, 0.5, 2, 25.0, 30.0),
        selection=RecommendationSelectionSettings(
            default_top_k=6,
            maximum_top_k=12,
            maximum_per_industry=2,
            observation_margin=5.0,
            thresholds={"tomorrow": 72.0, "d25": 70.0},
        ),
        dimension_weights={
            strategy: {
                **{name: 0.25 for name in DIMENSION_NAMES if name != "industry_policy"},
                "industry_policy": 0.0,
            }
            for strategy in Strategy
        },
        risk_rules=risk_rules,
        board_policy_version="fixture",
        board_candidate_weights={},
        board_local_strategy_weights={},
        candidate_component_weights={},
        local_component_weights={},
    )


@pytest.fixture
def application_feature_factory():
    def build(code: str, observed_at: datetime, *, industry: str = "工业") -> FeatureSnapshot:
        quote = MarketQuote(
            code=code,
            name=f"测试{code}",
            price=12.0,
            previous_close=11.65,
            open_price=11.8,
            high=12.2,
            low=11.7,
            pct_change=3.0,
            change_5m=1.0,
            speed=0.8,
            volume_ratio=2.0,
            turnover_rate=3.0,
            amount=300_000_000.0,
            amplitude=4.0,
            market_cap=30_000_000_000.0,
            industry=industry,
            source="fixture",
            source_time=observed_at,
            received_time=observed_at,
            data_version=f"fixture:{observed_at.isoformat()}",
        )
        values = {
            "amount_median_20d": 200_000_000.0,
            "amount_percentile_20d": 75.0,
            "speed_percentile": 75.0,
            "relative_strength_3d": 75.0,
            "relative_strength_5d": 75.0,
            "relative_strength_10d": 70.0,
            "relative_strength_20d": 70.0,
            "industry_strength": 70.0,
            "industry_breadth": 70.0,
            "industry_trend": 70.0,
            "news_sentiment": 60.0,
            "evidence_freshness": 70.0,
            "market_breadth": 60.0,
            "low_volatility_score": 70.0,
            "low_drawdown_score": 70.0,
            "low_crowding_score": 70.0,
            "volatility_20d": 2.0,
            "max_drawdown_20d": -8.0,
            "atr20_pct": 2.0,
            "ma5": 11.9,
            "ma10": 11.8,
            "ma20": 11.6,
            "ma20_slope_pct": 1.0,
            "volume_to_5d_average": 1.0,
            "prior_high_20d": 12.8,
            "breakout_deviation_pct": -6.25,
            "entry_quality": 70.0,
            "price_volume_confirmation": 70.0,
            "moderate_daily_return": 75.0,
            "ma20_60_position": 75.0,
            "ma20_60_structure": 75.0,
            "ma_slope": 70.0,
            "breakout_20d": 65.0,
            "risk_adjusted_return_20d": 70.0,
            "upward_consistency": 70.0,
            "capacity_score": 80.0,
            "moderate_amplitude": 75.0,
            "limit_distance_safety": 75.0,
            "tail_return_30m_pct": 0.8,
            "tail_return_30m": 70.0,
            "tail_volume_ratio_raw": 1.4,
            "tail_volume_ratio": 70.0,
            "close_location": 75.0,
            "price_executability": 75.0,
            "ma20_deviation_inverse": 70.0,
            "return_20d": 10.0,
            "trend_score": 70.0,
            "value_score": 70.0,
            "growth_score": 70.0,
            "quality_score": 70.0,
            "industry_policy_score": 70.0,
            "risk_protection_score": 70.0,
            "limit_proximity": 0.3,
            "price_volume_divergence": 0.0,
            "financial_deterioration": 0.0,
            "reduction_or_unlock": 0.0,
            "shareholder_reduction_level": 0.0,
            "unlock_risk": 0.0,
            "pledge_risk": 0.0,
            "negative_announcement_level": 0.0,
            "major_shareholder_reduction": 0.0,
            "financial_fraud_history": 0.0,
            "official_investigation_history": 0.0,
            "major_illegal_history": 0.0,
            "fund_occupation_history": 0.0,
            "illegal_guarantee_history": 0.0,
            "forced_delisting_risk": 0.0,
            "corporate_risk_history_unavailable": 0.0,
            "trend_breakdown": 0.0,
            "short_term_overheat": 0.0,
            "intraday_reversal": 0.0,
        }
        return FeatureSnapshot(quote=quote, values=values, observed_at=observed_at, history_days=60)

    return build


@pytest.fixture
def utc_now() -> datetime:
    return datetime(2026, 7, 16, 2, 0, tzinfo=timezone.utc)
