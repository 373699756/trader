from __future__ import annotations

from trader.application.market_data.feature_computation import build_feature_computation_plan
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
