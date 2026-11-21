"""D25 V3 industry L2 Logistic fit on the matured D25 hit event."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np

from trader.recommendation.application.ports.loaded_profile import ModelInput
from trader.training.infra.d25.artifact_values import D25ArtifactNumber, D25ArtifactObject, D25ArtifactVector
from trader.training.infra.logistic_fitting import FitBinaryLogistic
from trader.training.infra.fitting_settings import RidgeFittingParameters
from trader.training.infra.sample_index import TrainingSampleMatrix

ALGORITHM = "l2_logistic"


@dataclass(frozen=True, slots=True)
class D25LogisticModel:
    means: tuple[float, ...]
    scales: tuple[float, ...]
    intercept: float
    weights: tuple[float, ...]


def FitD25Logistic(
    training: TrainingSampleMatrix, parameters: RidgeFittingParameters, check: Callable[[], None],
) -> tuple[D25LogisticModel, Callable[[np.ndarray], np.ndarray]]:
    means = training.features.mean(axis=0)
    scale = training.features.std(axis=0)
    scales = np.where(scale > 1e-12, scale, 1.0)
    intercept, weights = FitBinaryLogistic((training.features - means) / scales, training.labels, parameters.penalty, check)

    def predict(features: np.ndarray) -> np.ndarray:
        return np.asarray(intercept + (features - means) / scales @ weights)

    return D25LogisticModel(tuple(means), tuple(scales), intercept, tuple(weights)), predict


def DecodeD25Logistic(value: object, width: int) -> D25LogisticModel:
    raw = D25ArtifactObject(value, {"means", "scales", "intercept", "weights"})
    means, scales = D25ArtifactVector(raw["means"], width), D25ArtifactVector(raw["scales"], width)
    if min(scales) <= 0:
        raise ValueError("d25_probability_transform_invalid")
    return D25LogisticModel(means, scales, D25ArtifactNumber(raw["intercept"]), D25ArtifactVector(raw["weights"], width))


def LoadD25LogisticScorer(model: D25LogisticModel) -> Callable[[Sequence[ModelInput]], np.ndarray]:
    means, scales, weights = np.asarray(model.means), np.asarray(model.scales), np.asarray(model.weights)

    def scores(items: Sequence[ModelInput]) -> np.ndarray:
        values = np.asarray([item.alpha_features for item in items])
        return np.asarray(model.intercept + (values - means) / scales @ weights)

    return scores
