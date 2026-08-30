"""Typed production Tomorrow model boundary."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, Protocol

TomorrowScoringProfile = Literal["p1", "p2"]


@dataclass(frozen=True)
class TomorrowModelInput:
    code: str
    alpha_features: tuple[float, ...]

    def __post_init__(self) -> None:
        if len(self.code) != 6 or not self.code.isdigit():
            raise ValueError("Tomorrow model input code is invalid")
        if not self.alpha_features or any(not math.isfinite(value) for value in self.alpha_features):
            raise ValueError("Tomorrow model alpha features must be non-empty and finite")


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

    def predict(self, inputs: tuple[TomorrowModelInput, ...]) -> tuple[TomorrowModelPrediction, ...]: ...


@dataclass(frozen=True)
class TomorrowModelRuntimeStatus:
    active: bool
    profile_id: TomorrowScoringProfile
    model_id: str
    model_hash: str
    scoring_version: str
    activation_basis: Literal["manual_user_override"]
    historical_status: Literal["historical_rejected", "historical_unavailable"]
    historical_failure_reasons: tuple[str, ...]
    monitoring_mode: Literal["automatic_t1_outcome_settlement"]
    automatic_model_update: bool
    loss_probability_status: Literal["not_modeled"]


__all__ = [
    "TomorrowModelInput",
    "TomorrowModelPrediction",
    "TomorrowModelPredictorPort",
    "TomorrowModelRuntimeStatus",
    "TomorrowScoringProfile",
]
