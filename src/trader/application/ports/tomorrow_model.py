"""Typed production Tomorrow model boundary."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
from dataclasses import dataclass
from typing import Protocol

from trader.application.ports.model_scoring import ScoringProfileRuntimeStatus
from trader.domain.recommendation.model_scoring.profile_identity import ScoringProfileId

TomorrowScoringProfile = ScoringProfileId


@dataclass(frozen=True)
class TomorrowHistoricalP2ModelArtifact:
    candidate_id: str
    feature_ids: tuple[str, ...]
    transformer_means: tuple[float, ...]
    transformer_scales: tuple[float, ...]
    linear_intercept: float
    linear_coefficients: tuple[float, ...]
    lightgbm_model: str
    lightgbm_best_iteration: int
    training_rows: int
    internal_validation_rows: int
    # This schema identity is part of the immutable packaged artifact.  The
    # ``_v1`` suffix is historical data identity, not a runtime scoring
    # profile, so it must remain unchanged while the active profile is ``v2``.
    schema_version: str = "score_tomorrow_historical_p2_model_v1"
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        width = len(self.feature_ids)
        if (
            self.candidate_id != "daily_reconstructible_ensemble_v1"
            or width < 1
            or len(set(self.feature_ids)) != width
            or len(self.transformer_means) != width
            or len(self.transformer_scales) != width
            or len(self.linear_coefficients) != width
            or not self.lightgbm_model
            or self.lightgbm_best_iteration < 1
            or self.training_rows < 1
            or not 1 <= self.internal_validation_rows < self.training_rows
            or self.schema_version != "score_tomorrow_historical_p2_model_v1"
        ):
            raise ValueError("Tomorrow P2 model artifact identity is invalid")
        numeric = (
            *self.transformer_means,
            *self.transformer_scales,
            self.linear_intercept,
            *self.linear_coefficients,
        )
        if any(not math.isfinite(value) for value in numeric) or any(value <= 0.0 for value in self.transformer_scales):
            raise ValueError("Tomorrow P2 model artifact parameters are invalid")
        payload = {field.name: getattr(self, field.name) for field in dataclasses.fields(self) if field.init}
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        object.__setattr__(self, "content_hash", hashlib.sha256(encoded.encode()).hexdigest())


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

    def predict(self, inputs: tuple[TomorrowModelInput, ...]) -> tuple[TomorrowModelPrediction, ...]: ...


TomorrowModelRuntimeStatus = ScoringProfileRuntimeStatus


__all__ = [
    "TomorrowHistoricalP2ModelArtifact",
    "TomorrowModelInput",
    "TomorrowModelPrediction",
    "TomorrowModelPredictorPort",
    "TomorrowModelRuntimeStatus",
    "TomorrowScoringProfile",
]
