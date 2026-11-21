"""Calibration of the D25 hit probability from the observed label partitions."""

from collections.abc import Callable

import numpy as np

from trader.training.infra.logistic_fitting import FitBinaryLogistic


def HasD25ProbabilityLabels(labels: np.ndarray) -> bool:
    return bool(np.any(labels == 0) and np.any(labels == 1))


def CalibrateD25Probability(scores: np.ndarray, labels: np.ndarray, check: Callable[[], None]) -> tuple[float, float]:
    if not HasD25ProbabilityLabels(labels):
        raise ValueError("d25_probability_calibration_labels_insufficient")
    intercept, slopes = FitBinaryLogistic(scores[:, None], labels, 0.001, check)
    slope = float(slopes[0])
    if not np.isfinite(slope) or slope <= 0:
        raise ValueError("d25_probability_calibration_not_monotone")
    return intercept, slope
