"""Independent Tomorrow fitting and input identities."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from trader.infra.serialization.canonical import ContentHash
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.training.domain.v1.learned_rank import V1TrainingSettings
from trader.training.infra.implementation_identity import TrainingImplementationIdentity
from trader.training.infra.model_fitting import ModelFittingParameters
from trader.training.infra.profile.v1.settings import LoadV1TrainingSettings
from trader.training.infra.training_settings import LoadProfileTrainingSettings


def TomorrowAlgorithm(profile: ScoringProfileId) -> str:
    return {"v1": "lambdarank", "v2": "lightgbm_binary", "v3": "l2_logistic", "v5": "tcn_bce"}[profile]


@dataclass(frozen=True, slots=True)
class TomorrowFitSettings:
    profile: ScoringProfileId
    fit_dates: int
    parameters: V1TrainingSettings | ModelFittingParameters

    @property
    def identity(self) -> str:
        return ContentHash((self, TomorrowAlgorithm(self.profile), TrainingImplementationIdentity()))


def LoadTomorrowFitSettings(project_root: Path, profile: ScoringProfileId) -> TomorrowFitSettings:
    if profile == "v1":
        settings = LoadV1TrainingSettings(project_root)
        return TomorrowFitSettings(profile, settings.fit_dates, settings)
    industry = LoadProfileTrainingSettings(project_root, profile)
    return TomorrowFitSettings(profile, industry.fit_dates, industry.fitting)
