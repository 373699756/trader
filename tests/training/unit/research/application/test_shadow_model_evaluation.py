from __future__ import annotations


from dataclasses import replace

from datetime import timedelta


import pytest

from trader.training.application.shadow_model_evaluation import TomorrowShadowModelEvaluator


from trader.training.application.shadow_model_report import ShadowSettlementLabel




from tests.helpers.training.research.application.shadow_model_evaluation import (
    START,
    _RecordingTrainer,
    _labeled_day,
)


def test_shadow_model_evaluation_use_walk_forward_embargo_same_data_and_complete_predictions() -> None:
    linear = _RecordingTrainer("linear")
    lightgbm = _RecordingTrainer("lightgbm")
    service = TomorrowShadowModelEvaluator((linear, lightgbm))
    days = tuple(day for index in range(66) for day in (_labeled_day(index, "tomorrow"), _labeled_day(index, "d25")))

    report = service.build(days)

    assert report.schema_version == "score_tomorrow_shadow_report"
    assert report.feature_version == "score_tomorrow_point_in_time_features"
    assert report.random_seed == 20260828
    assert report.cost_bps == 20
    assert report.production_authority is False
    assert len(report.content_hash) == 64
    assert {fold.window_mode for fold in report.folds} == {"expanding", "rolling"}
    assert {fold.horizon for fold in report.folds} == {"tomorrow", "d25"}
    d25_first = min(item.prediction_date for item in report.predictions if item.horizon == "d25")
    assert d25_first >= START + timedelta(days=60)
    assert all(len(fold.net_model_hash) == 64 and len(fold.severe_model_hash) == 64 for fold in report.folds)
    assert all(0.0 <= item.linear_severe_probability <= 1.0 for item in report.predictions)
    assert all(0.0 <= item.lightgbm_severe_probability <= 1.0 for item in report.predictions)
    assert len(linear.requests) == len(lightgbm.requests)
    assert linear.requests == lightgbm.requests


def test_shadow_report_is_order_independent_and_never_authorizes_production() -> None:
    days = tuple(day for index in range(66) for day in (_labeled_day(index, "tomorrow"), _labeled_day(index, "d25")))

    first = TomorrowShadowModelEvaluator((_RecordingTrainer("linear"), _RecordingTrainer("lightgbm"))).build(days)
    second = TomorrowShadowModelEvaluator((_RecordingTrainer("lightgbm"), _RecordingTrainer("linear"))).build(
        tuple(reversed(days))
    )

    assert first == second
    assert first.production_authority is False
    with pytest.raises(ValueError, match="training identity"):
        replace(first, spec_hash="f" * 64)
    with pytest.raises(ValueError, match="must be unique"):
        replace(first, predictions=(first.predictions[0], first.predictions[0]))
    with pytest.raises(ValueError, match="security identity"):
        replace(first.predictions[0], board="invalid")


def test_shadow_settlement_rejects_horizon_and_observation_lag_mismatch() -> None:
    basis = _labeled_day(0, "tomorrow").settlements[0].basis

    with pytest.raises(ValueError, match="horizon and observation lag"):
        ShadowSettlementLabel(horizon="d25", observation_lag=1, basis=basis)


def test_shadow_model_evaluation_reject_cross_horizon_feature_drift() -> None:
    tomorrow = _labeled_day(0, "tomorrow")
    d25 = _labeled_day(0, "d25")
    drifted = replace(d25, features=replace(d25.features, context_hash="f" * 64))

    with pytest.raises(ValueError, match="same point-in-time feature batch"):
        TomorrowShadowModelEvaluator((_RecordingTrainer("linear"), _RecordingTrainer("lightgbm"))).build(
            (tomorrow, drifted)
        )
    with pytest.raises(ValueError, match="same feature dates"):
        TomorrowShadowModelEvaluator((_RecordingTrainer("linear"), _RecordingTrainer("lightgbm"))).build(
            (tomorrow, _labeled_day(1, "d25"))
        )
