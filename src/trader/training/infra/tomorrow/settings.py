"""Independent Tomorrow fitting and input identities."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from trader.infra.serialization.canonical import ContentHash
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.training.domain.v1.learned_rank import V1TrainingSettings
from trader.training.infra.implementation_identity import LaneTrainingImplementationIdentity
from trader.training.infra.fitting_settings import (
    LightGBMFittingParameters, LoadIndustryFittingSettings, ModelFittingParameters, RidgeFittingParameters,
)
from trader.training.infra.learned_rank_settings import LoadV1TrainingSettings
from trader.training.infra.tomorrow.v1.model import ALGORITHM as V1_ALGORITHM
from trader.training.infra.tomorrow.v2.model import ALGORITHM as V2_ALGORITHM
from trader.training.infra.tomorrow.v3.model import ALGORITHM as V3_ALGORITHM
from trader.training.infra.tomorrow.v5.model import ALGORITHM as V5_ALGORITHM


def TomorrowAlgorithm(profile: ScoringProfileId) -> str:
    return {"v1": V1_ALGORITHM, "v2": V2_ALGORITHM, "v3": V3_ALGORITHM, "v5": V5_ALGORITHM}[profile]


@dataclass(frozen=True, slots=True)
class TomorrowFitSettings:
    profile: ScoringProfileId
    fit_dates: int
    parameters: V1TrainingSettings | ModelFittingParameters

    @property
    def fitting_identity(self) -> str:
        parameters = self.parameters
        effective: tuple[object, ...]
        if isinstance(parameters, (V1TrainingSettings, LightGBMFittingParameters)):
            effective = (
                parameters.learning_rate, parameters.max_depth, parameters.num_leaves, parameters.minimum_leaf_rows,
                parameters.boosting_rounds, parameters.maximum_bins, parameters.histogram_pool_mib, parameters.seed,
            )
            if isinstance(parameters, V1TrainingSettings):
                effective += (
                    parameters.early_stopping_dates, parameters.calibration_dates, parameters.validation_dates,
                    parameters.embargo_dates, parameters.minimum_training_rows, parameters.early_stopping_rounds,
                )
        elif isinstance(parameters, RidgeFittingParameters):
            effective = (parameters.penalty,)
        else:
            effective = (
                parameters.architecture, parameters.dropout, parameters.learning_rate, parameters.weight_decay,
                parameters.batch_size, parameters.epochs, parameters.seed, parameters.compute_threads,
            )
        return ContentHash((self.profile, TomorrowAlgorithm(self.profile), self.fit_dates, effective))

    @property
    def identity(self) -> str:
        return ContentHash((self.fitting_identity, LaneTrainingImplementationIdentity(Strategy.TOMORROW, self.profile)))


def LoadTomorrowFitSettings(project_root: Path, profile: ScoringProfileId) -> TomorrowFitSettings:
    if profile == "v1":
        settings = LoadV1TrainingSettings(project_root)
        return TomorrowFitSettings(profile, settings.fit_dates, settings)
    fit_dates, fitting = LoadIndustryFittingSettings(project_root, profile)
    return TomorrowFitSettings(profile, fit_dates, fitting)
