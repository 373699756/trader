from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest

from trader.domain.research.limited_factor_family import (
    FactorFamilyCandidateSeries,
    LimitedFactorCandidate,
    LimitedFactorFamilySpec,
    build_intraday_price_volume_family_spec,
    evaluate_limited_factor_family,
    insufficient_limited_factor_family_report,
)

HASH = "a" * 64
DATES = tuple(date(2024, 1, 2) + timedelta(days=index) for index in range(10))


def _spec() -> LimitedFactorFamilySpec:
    return LimitedFactorFamilySpec(
        family_id="intraday_price_volume_path",
        control_feature_ids=(
            "return_1d",
            "return_3d",
            "return_5d",
            "residual_momentum_20d",
            "residual_momentum_40d",
            "residual_momentum_60d",
        ),
        candidates=(
            LimitedFactorCandidate("existing_six_alpha", "control", "unitless", "control"),
            LimitedFactorCandidate("tail_volume_share", "tail_volume_share", "ratio"),
        ),
        selected_candidate_id="tail_volume_share",
        development_dates=DATES[:5],
        confirmation_dates=DATES[5:],
        bootstrap_repetitions=1_000,
    )


def _series(**overrides: object) -> FactorFamilyCandidateSeries:
    defaults: dict[str, object] = {
        "candidate_id": "tail_volume_share",
        "trade_dates": DATES,
        "overall_coverage": 0.99,
        "board_coverages": (0.99, 0.99, 0.99),
        "paired_increment_20bp": (0.03,) * 10,
        "paired_increment_50bp": (0.02,) * 10,
        "paired_increment_100bp": (0.01,) * 10,
        "rank_ic_delta": (0.01,) * 10,
        "top10_increment": (0.03,) * 10,
        "top20_increment": (0.025,) * 10,
        "top50_increment": (0.02,) * 10,
        "quintile_spread": (0.02,) * 10,
        "severe_loss_rate_delta": (-0.01,) * 10,
        "maximum_drawdown_delta": (-0.01,) * 10,
        "turnover_delta": (-0.01,) * 10,
        "capacity_shortfall_delta": (-0.01,) * 10,
        "market_state_directions": (1, 1, 1),
    }
    defaults.update(overrides)
    return FactorFamilyCandidateSeries(**defaults)  # type: ignore[arg-type]


def test_canonical_family_freezes_six_alpha_control_and_one_selected_intraday_candidate() -> None:
    spec = build_intraday_price_volume_family_spec(
        development_dates=DATES[:5],
        confirmation_dates=DATES[5:],
        bootstrap_repetitions=100,
    )

    assert spec.family_id == "intraday_price_volume_path"
    assert len(spec.control_feature_ids) == 6
    assert spec.candidates[0].candidate_id == "existing_six_alpha"
    assert spec.selected_candidate_id == "tail_volume_share"
    assert spec.anchor == "14:50_point_in_time"


def test_limited_factor_family_confirms_only_the_preregistered_candidate() -> None:
    report = evaluate_limited_factor_family(
        _spec(),
        dataset_report_hash=HASH,
        dataset_manifest_hash="b" * 64,
        recall_report_hash="c" * 64,
        series=(_series(),),
    )

    assert report.state == "factor_family_confirmed"
    assert report.selected_candidate_id == "tail_volume_share"
    assert report.evidence[0].holm.rejected_null
    assert report.evidence[0].bootstrap_20bp.confidence_lower > 0.0
    assert not report.terminal_holdout_opened
    assert not report.production_authority


def test_limited_factor_family_uses_only_the_sealed_confirmation_dates_for_gates() -> None:
    report = evaluate_limited_factor_family(
        _spec(),
        dataset_report_hash=HASH,
        dataset_manifest_hash="b" * 64,
        recall_report_hash="c" * 64,
        series=(
            _series(
                paired_increment_20bp=(-1.0,) * 5 + (0.03,) * 5,
                paired_increment_50bp=(-1.0,) * 5 + (0.02,) * 5,
                rank_ic_delta=(-1.0,) * 5 + (0.01,) * 5,
            ),
        ),
    )

    assert report.state == "factor_family_confirmed"
    assert report.evidence[0].bootstrap_20bp.sample_count == 5


@pytest.mark.parametrize(
    ("overrides", "reason"),
    (
        ({"overall_coverage": 0.94}, "coverage_below_required"),
        ({"severe_loss_rate_delta": (0.01,) * 10}, "severe_loss_worsened"),
        ({"market_state_directions": (1, -1, 1)}, "market_state_direction_reversed"),
    ),
)
def test_limited_factor_family_rejects_failed_quality_or_risk_gates(overrides: dict[str, object], reason: str) -> None:
    report = evaluate_limited_factor_family(
        _spec(),
        dataset_report_hash=HASH,
        dataset_manifest_hash="b" * 64,
        recall_report_hash="c" * 64,
        series=(_series(**overrides),),
    )

    assert report.state == "historical_rejected"
    assert report.selected_candidate_id is None
    assert reason in report.evidence[0].failure_reasons


def test_limited_factor_family_rejects_unregistered_or_incomplete_evidence() -> None:
    with pytest.raises(ValueError, match="complete preregistered challenger family"):
        evaluate_limited_factor_family(
            replace(
                _spec(),
                candidates=(
                    LimitedFactorCandidate("existing_six_alpha", "control", "unitless", "control"),
                    LimitedFactorCandidate("tail_volume_share", "tail_volume_share", "ratio"),
                    LimitedFactorCandidate("vwap_deviation", "vwap_deviation", "decimal_return"),
                ),
            ),
            dataset_report_hash=HASH,
            dataset_manifest_hash="b" * 64,
            recall_report_hash="c" * 64,
            series=(_series(),),
        )


def test_limited_factor_family_insufficient_report_has_no_partial_evidence() -> None:
    report = insufficient_limited_factor_family_report(
        _spec(),
        dataset_report_hash=HASH,
        dataset_manifest_hash=None,
        recall_report_hash="c" * 64,
        reason="candidate_recall_unavailable",
    )

    assert report.state == "historical_data_insufficient"
    assert report.evidence == ()
    assert report.selected_candidate_id is None
    assert report.failure_reasons == ("candidate_recall_unavailable",)
