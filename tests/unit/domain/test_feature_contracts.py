from __future__ import annotations

import pytest

from trader.domain.market.feature_contracts import (
    FEATURE_SPEC_CATALOG,
    TOMORROW_MODEL_FEATURE_MANIFEST,
    TOMORROW_RAW_ALPHA_FEATURE_MANIFEST,
    TOMORROW_RESEARCH_FEATURE_MANIFEST,
    FeatureId,
    QfqPriceAnchors,
    calculate_tomorrow_qfq_alpha,
)


def test_catalog_owns_model_units_order_missing_policy_and_dependencies() -> None:
    manifest = TOMORROW_MODEL_FEATURE_MANIFEST

    assert manifest.names == (
        "qfq_return_1d",
        "qfq_return_3d",
        "qfq_return_5d",
        "qfq_residual_momentum_20d_skip5",
        "qfq_residual_momentum_40d_skip5",
        "qfq_residual_momentum_60d_skip5",
    )
    assert manifest.units == ("decimal_return",) * 6
    assert manifest.missing_policies == ("reject_model_input",) * 6
    assert len(manifest.content_hash) == 64
    assert manifest.catalog_hash == FEATURE_SPEC_CATALOG.content_hash
    assert FEATURE_SPEC_CATALOG.require("qfq_residual_momentum_20d_skip5").dependencies == (
        FeatureId("qfq_momentum_20d_skip5"),
        FeatureId("market_cross_section"),
        FeatureId("board_cross_section"),
        FeatureId("industry_cross_section"),
        FeatureId("qfq_average_amount_20d"),
    )


def test_qfq_calculators_preserve_production_skip_five_semantics_and_missing_values() -> None:
    values = calculate_tomorrow_qfq_alpha(
        QfqPriceAnchors(
            current_close=120.0,
            lagged_closes=((1, 100.0), (3, 80.0), (5, 60.0), (20, 50.0), (40, 40.0), (60, 30.0)),
        )
    )
    by_name = {item.feature_id.value: item.value for item in values}

    assert tuple(by_name) == TOMORROW_RAW_ALPHA_FEATURE_MANIFEST.names
    assert by_name["qfq_return_1d"] == pytest.approx(0.2)
    assert by_name["qfq_return_3d"] == pytest.approx(0.5)
    assert by_name["qfq_return_5d"] == pytest.approx(1.0)
    assert by_name["qfq_momentum_20d_skip5"] == pytest.approx(0.2)
    assert by_name["qfq_momentum_40d_skip5"] == pytest.approx(0.5)
    assert by_name["qfq_momentum_60d_skip5"] == pytest.approx(1.0)

    missing = calculate_tomorrow_qfq_alpha(QfqPriceAnchors(120.0, ((1, 100.0),)))
    assert missing[0].value == pytest.approx(0.2)
    assert all(item.value is None for item in missing[1:])

    invalid = calculate_tomorrow_qfq_alpha(QfqPriceAnchors(float("nan"), ((1, 100.0), (5, 0.0))))
    assert all(item.value is None for item in invalid)


def test_manifest_rejects_unknown_duplicate_and_out_of_order_model_features() -> None:
    with pytest.raises(KeyError, match="unknown feature"):
        FEATURE_SPEC_CATALOG.manifest((FeatureId("not_registered"),))
    with pytest.raises(ValueError, match="unique"):
        FEATURE_SPEC_CATALOG.manifest((FeatureId("qfq_return_1d"), FeatureId("qfq_return_1d")))

    reversed_ids = tuple(reversed(TOMORROW_MODEL_FEATURE_MANIFEST.feature_ids))
    assert FEATURE_SPEC_CATALOG.manifest(reversed_ids).content_hash != TOMORROW_MODEL_FEATURE_MANIFEST.content_hash


def test_research_manifest_is_the_single_shadow_feature_order() -> None:
    assert TOMORROW_RESEARCH_FEATURE_MANIFEST.names == (
        "residual_reversal_1d",
        "residual_reversal_3d",
        "residual_reversal_5d",
        "residual_momentum_20_5",
        "residual_momentum_40_5",
        "residual_momentum_60_5",
        "overnight_gap",
        "intraday_return",
        "morning_return",
        "afternoon_return",
        "tail_return_30m",
        "close_location",
        "tail_amount_share",
    )
