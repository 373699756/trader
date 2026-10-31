"""Industry batches preserve the previously published scalar prediction formula."""

import math
from dataclasses import replace

import lightgbm as lgb
import numpy as np
import pytest

from tests.unit.training.infra.profile.v3.test_profile import _documents
from trader.recommendation.application.ports.loaded_profile import ModelInput
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.selection.execution_cost import ExecutionCostPolicy
from trader.training.infra.model_bundles.bundle_codec import (
    DecodeHeadBundle,
    TrainedHeadModelBundle,
    TrainedLightGBMModelSnapshot,
    TrainedRidgeModelSnapshot,
)
from trader.training.infra.model_bundles.contracts import TrainedProfileContract
from trader.training.infra.model_bundles.predictor import TrainedHeadPredictor
from trader.training.infra.model_fitting import _ValidationPredictor
from trader.training.infra.profile.v2.contracts import V2_TRAINING_PROFILE
from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE


def _bundle(profile: TrainedProfileContract, strategy: Strategy) -> TrainedHeadModelBundle:
    contract = profile.head_for_strategy(strategy)
    documents = _documents(contract, profile)
    bundle = DecodeHeadBundle(*documents, strategy, profile)
    model = bundle.industries[0][1]
    width = len(bundle.feature_ids)
    first = replace(
        model,
        transformer_means=tuple(index / 100 for index in range(width)),
        transformer_scales=tuple(1 + index / 10 for index in range(width)),
        calibration_intercept=0.001,
        calibration_slope=1.1,
    )
    second = replace(first, calibration_intercept=-0.002, calibration_slope=0.7)
    return replace(bundle, industries=(("银行", first), ("制造", second)))


def _scalar_predictions(bundle: TrainedHeadModelBundle, inputs: tuple[ModelInput, ...]) -> tuple[float, ...]:
    models = dict(bundle.industries)
    boosters = {
        industry: lgb.Booster(model_str=model.lightgbm_model)
        for industry, model in bundle.industries
        if isinstance(model, TrainedLightGBMModelSnapshot)
    }
    output = []
    for item in inputs:
        model = models[item.industry]
        matrix = np.asarray((item.alpha_features,), dtype=np.float64)
        standardized = (matrix - np.asarray(model.transformer_means)) / np.asarray(model.transformer_scales)
        if isinstance(model, TrainedLightGBMModelSnapshot):
            predicted = float(
                boosters[item.industry].predict(
                    standardized, num_iteration=model.lightgbm_best_iteration, num_threads=1
                )[0]
            )
        else:
            assert isinstance(model, TrainedRidgeModelSnapshot)
            predicted = float(model.ridge_intercept + standardized[0] @ np.asarray(model.ridge_coefficients))
        output.append(model.calibration_intercept + model.calibration_slope * predicted)
    return tuple(output)


@pytest.mark.parametrize("profile", (V2_TRAINING_PROFILE, V3_TRAINING_PROFILE))
@pytest.mark.parametrize("strategy", (Strategy.TOMORROW, Strategy.D25))
def test_industry_batches_match_scalar_formula_and_restore_input_order(profile, strategy, monkeypatch) -> None:
    bundle = _bundle(profile, strategy)
    predictor = TrainedHeadPredictor(profile.profile_id, bundle, strategy)
    inputs = tuple(
        ModelInput(
            f"{600000 + index:06d}",
            tuple((index + column) / 1000 for column in range(len(bundle.feature_ids))),
            "制造" if index == 128 else "银行",
        )
        for index in reversed(range(258))
    )
    expected = _scalar_predictions(bundle, inputs)
    calls = []
    if profile.algorithm == "lightgbm":
        original = lgb.Booster.predict

        def observe(booster, matrix, *args, **kwargs):
            calls.append(len(matrix))
            return original(booster, matrix, *args, **kwargs)

        monkeypatch.setattr(lgb.Booster, "predict", observe)
    else:
        original_ridge = TrainedHeadPredictor._predict_ridge_batch

        def observe_ridge(adapter, industry, matrix):
            calls.append(len(matrix))
            return original_ridge(adapter, industry, matrix)

        monkeypatch.setattr(TrainedHeadPredictor, "_predict_ridge_batch", observe_ridge)

    actual = predictor.predict(inputs)

    assert tuple(item.code for item in actual) == tuple(item.code for item in inputs)
    predictions = tuple(item.predicted_excess_return for item in actual)
    if profile.algorithm == "ridge":
        assert predictions == expected
    else:
        assert predictions == pytest.approx(expected, rel=1e-12, abs=1e-12)
    assert sorted(calls) == [1, 1, 256]
    for parameters in predictor._arrays.values():
        assert not parameters.means.flags.writeable
        assert not parameters.scales.flags.writeable
        with pytest.raises(ValueError, match="read-only"):
            parameters.means[0] = 999
        if parameters.coefficients is not None:
            assert not parameters.coefficients.flags.writeable


@pytest.mark.parametrize("failure", ("industry", "width", "duplicate"))
def test_invalid_batches_fail_before_native_prediction(failure, monkeypatch) -> None:
    bundle = _bundle(V2_TRAINING_PROFILE, Strategy.TOMORROW)
    predictor = TrainedHeadPredictor("v2", bundle, Strategy.TOMORROW)
    item = ModelInput("600000", (0.01,) * len(bundle.feature_ids), "银行")
    other = replace(item, code="600001")
    if failure == "industry":
        other = replace(other, industry="uncovered")
    elif failure == "width":
        other = replace(other, alpha_features=(0.01,))
    else:
        other = item

    def unexpected_prediction(*_args, **_kwargs):
        raise AssertionError("an invalid batch must not call its native predictor")

    monkeypatch.setattr(lgb.Booster, "predict", unexpected_prediction)
    with pytest.raises(ValueError):
        predictor.predict((item, other))


@pytest.mark.parametrize("strategy", (Strategy.TOMORROW, Strategy.D25))
@pytest.mark.parametrize("boundary", ("below", "equal", "above"))
def test_ridge_batches_preserve_exact_cost_and_adjacent_float_eligibility(strategy, boundary) -> None:
    bundle = _bundle(V3_TRAINING_PROFILE, strategy)
    width = len(bundle.feature_ids)
    cost = ExecutionCostPolicy(0.002, 1.0).estimate(0.5)
    target = {
        "below": math.nextafter(cost, -math.inf),
        "equal": cost,
        "above": math.nextafter(cost, math.inf),
    }[boundary]
    original = bundle.industries[0][1]
    assert isinstance(original, TrainedRidgeModelSnapshot)
    model = replace(
        original,
        transformer_means=(0.0,) * width,
        transformer_scales=(1.0,) * width,
        ridge_intercept=target,
        ridge_coefficients=(1.0,) * width,
        calibration_intercept=0.0,
        calibration_slope=1.0,
    )
    bundle = replace(bundle, industries=(("银行", model),))
    # A matrix reduction can retain the middle term that the original row dot loses.
    inputs = tuple(
        ModelInput(f"{600000 + index:06d}", (1.0, 1e-16, -1.0, *(0.0,) * (width - 3)), "银行") for index in range(3)
    )
    expected = _scalar_predictions(bundle, inputs)
    assert expected == (target,) * 3

    actual = TrainedHeadPredictor("v3", bundle, strategy).predict(inputs)
    predicted = tuple(item.predicted_excess_return for item in actual)

    assert predicted == expected
    assert tuple(value - cost > 0.0 for value in predicted) == (boundary == "above",) * 3
    validation = _ValidationPredictor(model, 2).predict(
        np.asarray(tuple(item.alpha_features for item in inputs), dtype=np.float64)
    )
    np.testing.assert_array_equal(validation, np.asarray(predicted))
    assert tuple(value - cost > 0.0 for value in validation) == (boundary == "above",) * 3
