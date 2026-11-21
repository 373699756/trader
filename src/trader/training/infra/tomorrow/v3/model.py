"""Tomorrow V3 scaled L2 Logistic classifier."""

import math
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from trader.recommendation.domain.market.tomorrow_probability import TOMORROW_FEATURE_NAMES
from trader.training.infra.logistic_fitting import FitBinaryLogistic

ALGORITHM = "l2_logistic"


@dataclass(frozen=True, slots=True)
class LogisticTomorrowModel:
    intercept: float
    weights: tuple[float, ...]
    means: tuple[float, ...]
    scales: tuple[float, ...]

    def __post_init__(self) -> None:
        width = len(TOMORROW_FEATURE_NAMES)
        if (
            len(self.weights) != width or len(self.means) != width or len(self.scales) != width
            or not all(math.isfinite(value) for value in (self.intercept, *self.weights, *self.means, *self.scales))
            or any(value <= 0 for value in self.scales)
        ):
            raise ValueError("tomorrow_logistic_model_invalid")


def FitTomorrowLogistic(
    features: np.ndarray, labels: np.ndarray, penalty: float, check: Callable[[], None],
) -> LogisticTomorrowModel:
    means = features.mean(axis=0) if len(features) else np.zeros(features.shape[1])
    scales = features.std(axis=0) if len(features) else np.ones(features.shape[1])
    scales = np.where(scales > 1e-9, scales, 1.0)
    intercept, weights = FitBinaryLogistic((features - means) / scales, labels, penalty, check)
    return LogisticTomorrowModel(intercept, tuple(weights), tuple(means), tuple(scales))


def TomorrowLogisticScores(model: LogisticTomorrowModel, features: np.ndarray) -> np.ndarray:
    normalized = (features - np.asarray(model.means)) / np.asarray(model.scales)
    return np.asarray(normalized @ np.asarray(model.weights) + model.intercept, dtype=np.float64)
