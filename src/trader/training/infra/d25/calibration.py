"""Calibration of the D25 hit probability from the observed label partitions."""

from collections.abc import Callable
from datetime import date

import numpy as np

from trader.training.infra.logistic_fitting import FitBinaryLogistic
from trader.training.domain.d25_assessment import D25ProbabilityAssessment


class D25ProbabilityCalibrationError(ValueError):
    """The fitted probability model failed its independent calibration gate."""


def HasD25ProbabilityLabels(labels: np.ndarray) -> bool:
    return bool(np.any(labels == 0) and np.any(labels == 1))


def CalibrateD25Probability(scores: np.ndarray, labels: np.ndarray, check: Callable[[], None]) -> tuple[float, float]:
    if not HasD25ProbabilityLabels(labels):
        raise D25ProbabilityCalibrationError("d25_probability_calibration_labels_insufficient")
    intercept, slopes = FitBinaryLogistic(scores[:, None], labels, 0.001, check)
    slope = float(slopes[0])
    if not np.isfinite(slope) or slope <= 0:
        raise D25ProbabilityCalibrationError("d25_probability_calibration_not_monotone")
    return intercept, slope


def CalibratedD25Probabilities(scores: np.ndarray, calibration: tuple[float, float]) -> np.ndarray:
    intercept, slope = calibration
    logits = intercept + slope * scores
    if not np.isfinite(logits).all():
        raise ValueError("d25_probability_prediction_nonfinite")
    return np.exp(-np.logaddexp(0.0, -logits))


def AssessD25Probabilities(
    scores: np.ndarray, labels: np.ndarray, calibration: tuple[float, float], *,
    validation_dates: tuple[date, ...], scored_dates: set[date], sample_rows: int, labeled_rows: int,
    training_rows: int, training_positive_rows: int,
) -> D25ProbabilityAssessment:
    if scores.ndim != 1 or labels.shape != scores.shape or not np.isin(labels, (0, 1)).all():
        raise ValueError("d25_probability_assessment_inputs_invalid")
    probabilities = CalibratedD25Probabilities(scores, calibration)
    prevalence = training_positive_rows / training_rows
    return D25ProbabilityAssessment(
        validation_dates, tuple(sorted(scored_dates)), sample_rows, labeled_rows, len(labels), int(labels.sum()),
        training_rows, training_positive_rows,
        float(np.mean(np.square(probabilities - labels))) if len(labels) else None,
        float(np.mean(np.square(prevalence - labels))) if len(labels) else None,
    )
