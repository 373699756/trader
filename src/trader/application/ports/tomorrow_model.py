"""Typed production Tomorrow model boundary."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol

from trader.application.ports.model_scoring import ProfileEvidence, ScoringProfileRuntimeStatus
from trader.domain.recommendation.model_scoring.profile_identity import ScoringProfileId

TomorrowScoringProfile = ScoringProfileId


@dataclass(frozen=True)
class TomorrowModelInput:
    code: str
    alpha_features: tuple[float, ...]
    industry: str = ""

    def __post_init__(self) -> None:
        if len(self.code) != 6 or not self.code.isdigit():
            raise ValueError("Tomorrow model input code is invalid")
        if not self.alpha_features or any(not math.isfinite(value) for value in self.alpha_features):
            raise ValueError("Tomorrow model alpha features must be non-empty and finite")
        if not isinstance(self.industry, str):
            raise ValueError("Tomorrow model industry must be text")


@dataclass(frozen=True)
class TomorrowModelPrediction:
    code: str
    predicted_excess_return: float
    model_disagreement: float

    def __post_init__(self) -> None:
        if len(self.code) != 6 or not self.code.isdigit():
            raise ValueError("Tomorrow model prediction code is invalid")
        if not math.isfinite(self.predicted_excess_return):
            raise ValueError("Tomorrow model prediction must be finite")
        if not math.isfinite(self.model_disagreement) or self.model_disagreement < 0.0:
            raise ValueError("Tomorrow model disagreement must be finite and non-negative")


class TomorrowModelPredictorPort(Protocol):
    @property
    def profile_id(self) -> TomorrowScoringProfile: ...

    @property
    def model_id(self) -> str: ...

    @property
    def model_hash(self) -> str: ...

    @property
    def feature_ids(self) -> tuple[str, ...]: ...

    @property
    def industry_ids(self) -> tuple[str, ...]: ...

    @property
    def profile_evidence(self) -> ProfileEvidence: ...

    def predict(self, inputs: tuple[TomorrowModelInput, ...]) -> tuple[TomorrowModelPrediction, ...]: ...


TomorrowModelRuntimeStatus = ScoringProfileRuntimeStatus


__all__ = [
    "TomorrowModelInput",
    "TomorrowModelPrediction",
    "TomorrowModelPredictorPort",
    "TomorrowModelRuntimeStatus",
    "TomorrowScoringProfile",
]
