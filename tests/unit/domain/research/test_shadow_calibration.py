from __future__ import annotations

import math

from trader.training.domain.evaluation.shadow_calibration import (
    FitAffineCalibrator,
    FitLogisticModel,
    FitPlattCalibrator,
    FitRidgeModel,
)


def test_linear_models_and_calibrators_are_deterministic() -> None:
    features = tuple((float(index), float(index % 3)) for index in range(1, 31))
    net_targets = tuple(0.4 * row[0] - 0.2 * row[1] + 0.5 for row in features)
    severe_targets = tuple(1.0 if index % 4 == 0 else 0.0 for index in range(1, 31))

    ridge = FitRidgeModel(features, net_targets, ridge=1e-3)
    logistic = FitLogisticModel(features, severe_targets, ridge=1e-3)
    raw_net = ridge.predict(features)
    raw_probability = logistic.predict(features)
    affine = FitAffineCalibrator(raw_net, net_targets)
    platt = FitPlattCalibrator(raw_probability, severe_targets)

    assert ridge == FitRidgeModel(features, net_targets, ridge=1e-3)
    assert logistic == FitLogisticModel(features, severe_targets, ridge=1e-3)
    assert (
        max(abs(actual - expected) for actual, expected in zip(affine.predict(raw_net), net_targets, strict=True))
        < 0.01
    )
    assert all(0.0 <= value <= 1.0 and math.isfinite(value) for value in platt.predict(raw_probability))


def test_platt_calibration_handles_single_class_without_fake_certainty() -> None:
    calibrator = FitPlattCalibrator((0.1, 0.2, 0.3), (0.0, 0.0, 0.0))

    assert calibrator.predict((0.0, 0.5, 1.0)) == (0.2, 0.2, 0.2)
