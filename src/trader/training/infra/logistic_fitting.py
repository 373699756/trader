"""Numerically stable binary L2 logistic fitting and probability calibration."""

import math
from collections.abc import Callable

import numpy as np
from numpy.typing import NDArray

_FloatArray = NDArray[np.float64]


def FitBinaryLogistic(
    features: _FloatArray, labels: _FloatArray, ridge: float, check: Callable[[], None],
) -> tuple[float, _FloatArray]:
    if not (np.any(labels == 1) and np.any(labels == 0)):
        raise ValueError("probability_partition_requires_both_labels")
    params: _FloatArray = np.zeros(features.shape[1] + 1, dtype=np.float64)
    positive = float(labels.mean())
    params[0] = math.log(positive / (1 - positive))
    design: _FloatArray = np.empty((len(labels), len(params)), dtype=np.float64)
    design[:, 0] = 1
    design[:, 1:] = features
    penalty = np.eye(len(params)) * ridge
    penalty[0, 0] = 1e-8

    def loss(parameters: _FloatArray) -> float:
        scores = design @ parameters
        return float(np.sum(np.logaddexp(0, scores) - labels * scores) + parameters @ penalty @ parameters / 2)

    for _iteration in range(30):
        check()
        fitted = np.exp(-np.logaddexp(0.0, -(design @ params)))
        variance = fitted * (1 - fitted)
        hessian = design.T @ (design * variance[:, None]) + penalty
        gradient = design.T @ (fitted - labels) + penalty @ params
        step = np.linalg.solve(hessian, gradient)
        if np.max(np.abs(step)) < 1e-7:
            break
        baseline = loss(params)
        tolerance = 1e-12 * max(1.0, abs(baseline))
        for exponent in range(12):
            proposal = params - step / (2 ** exponent)
            improvement = baseline - loss(proposal)
            if improvement > tolerance:
                params = proposal
                break
            if abs(improvement) <= tolerance:
                return float(params[0]), params[1:]
        else:
            raise ValueError("probability_logistic_step_unstable")
    return float(params[0]), params[1:]
