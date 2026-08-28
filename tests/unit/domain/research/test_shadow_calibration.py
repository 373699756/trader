from __future__ import annotations

import math

from trader.domain.research.shadow_calibration import (
    fit_affine_calibrator,
    fit_logistic_model,
    fit_platt_calibrator,
    fit_ridge_model,
)


def test_linear_models_and_calibrators_are_deterministic() -> None:
    features = tuple((float(index), float(index % 3)) for index in range(1, 31))
    net_targets = tuple(0.4 * row[0] - 0.2 * row[1] + 0.5 for row in features)
    severe_targets = tuple(1.0 if index % 4 == 0 else 0.0 for index in range(1, 31))

    ridge = fit_ridge_model(features, net_targets, ridge=1e-3)
    logistic = fit_logistic_model(features, severe_targets, ridge=1e-3)
    raw_net = ridge.predict(features)
    raw_probability = logistic.predict(features)
    affine = fit_affine_calibrator(raw_net, net_targets)
    platt = fit_platt_calibrator(raw_probability, severe_targets)

    assert ridge == fit_ridge_model(features, net_targets, ridge=1e-3)
    assert logistic == fit_logistic_model(features, severe_targets, ridge=1e-3)
    assert (
        max(abs(actual - expected) for actual, expected in zip(affine.predict(raw_net), net_targets, strict=True))
        < 0.01
    )
    assert all(0.0 <= value <= 1.0 and math.isfinite(value) for value in platt.predict(raw_probability))


def test_platt_calibration_handles_single_class_without_fake_certainty() -> None:
    calibrator = fit_platt_calibrator((0.1, 0.2, 0.3), (0.0, 0.0, 0.0))

    assert calibrator.predict((0.0, 0.5, 1.0)) == (0.2, 0.2, 0.2)
