from __future__ import annotations

from trader.application.market_data.feature_computation import (
    FeatureFactRevision,
    affected_feature_stages,
    build_feature_computation_plan,
)
from trader.domain.market.feature_contracts import (
    TOMORROW_MODEL_FEATURE_MANIFEST,
    TOMORROW_RAW_ALPHA_FEATURE_MANIFEST,
)


def test_raw_alpha_computation_plan_groups_calculators_in_dependency_order() -> None:
    plan = build_feature_computation_plan(TOMORROW_RAW_ALPHA_FEATURE_MANIFEST)

    assert tuple(stage.calculator_group for stage in plan.stages) == ("daily_return", "skip_recent_momentum")
    assert plan.output_names == TOMORROW_RAW_ALPHA_FEATURE_MANIFEST.names
    assert plan.required_fact_ids == (
        "qfq_close_current",
        "qfq_close_lag_1",
        "qfq_close_lag_3",
        "qfq_close_lag_5",
        "qfq_close_lag_20",
        "qfq_close_lag_40",
        "qfq_close_lag_60",
    )


def test_model_plan_resolves_raw_features_before_cross_section_residuals() -> None:
    plan = build_feature_computation_plan(TOMORROW_MODEL_FEATURE_MANIFEST)

    assert tuple(stage.calculator_group for stage in plan.stages) == (
        "daily_return",
        "skip_recent_momentum",
        "cross_section_residual",
    )
    assert plan.output_names == TOMORROW_MODEL_FEATURE_MANIFEST.names
    assert set(plan.required_fact_ids) >= {
        "market_cross_section",
        "board_cross_section",
        "industry_cross_section",
        "qfq_average_amount_20d",
    }
    assert not hasattr(plan, "content_hash")


def test_fact_revisions_invalidate_only_transitively_dependent_groups() -> None:
    plan = build_feature_computation_plan(TOMORROW_MODEL_FEATURE_MANIFEST)
    baseline = tuple(FeatureFactRevision(fact_id, "same") for fact_id in plan.required_fact_ids)
    current_close_changed = tuple(
        FeatureFactRevision(item.fact_id, "changed" if item.fact_id == "qfq_close_current" else item.revision)
        for item in baseline
    )

    unchanged = affected_feature_stages(plan, baseline, baseline)
    changed = affected_feature_stages(plan, baseline, current_close_changed)

    assert unchanged.affected_groups == ()
    assert changed.dirty_fact_ids == ("qfq_close_current",)
    assert changed.affected_groups == ("daily_return",)


def test_shared_lag_revision_invalidates_momentum_and_its_residual_dependents() -> None:
    plan = build_feature_computation_plan(TOMORROW_MODEL_FEATURE_MANIFEST)
    baseline = tuple(FeatureFactRevision(fact_id, "same") for fact_id in plan.required_fact_ids)
    changed = tuple(
        FeatureFactRevision(item.fact_id, "changed" if item.fact_id == "qfq_close_lag_5" else item.revision)
        for item in baseline
    )

    invalidation = affected_feature_stages(plan, baseline, changed)

    assert invalidation.affected_groups == (
        "daily_return",
        "skip_recent_momentum",
        "cross_section_residual",
    )
