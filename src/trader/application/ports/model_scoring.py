"""Typed application boundary for strategy-aware model scoring."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Literal, Protocol

from trader.domain.market.models import FeatureSnapshot
from trader.domain.recommendation.model_scoring.profile_identity import ScoringProfileId
from trader.domain.recommendation.models import Strategy
from trader.domain.recommendation.strategies.composition import LocalScoreResult


class ScoringHeadInput(Protocol):
    """Minimal typed input accepted by a production strategy head."""

    @property
    def code(self) -> str: ...

    @property
    def strategy(self) -> Strategy: ...

    @property
    def observed_at(self) -> datetime: ...


class HeadPrediction(Protocol):
    """Prediction shape shared by every strategy head."""

    @property
    def code(self) -> str: ...

    @property
    def predicted_excess_return(self) -> float: ...

    @property
    def model_disagreement(self) -> float: ...


class HeadPredictorPort(Protocol):
    """A predictor owned by a profile, without loading or persistence duties."""

    @property
    def model_id(self) -> str: ...

    @property
    def model_hash(self) -> str: ...

    def predict(self, inputs: Sequence[ScoringHeadInput]) -> tuple[HeadPrediction, ...]: ...


@dataclass(frozen=True)
class ModelDiagnostics:
    predicted_excess_return_pct: float
    estimated_cost_pct: float
    predicted_net_excess_pct: float
    model_disagreement_pct: float


@dataclass(frozen=True)
class ModelScoreBatch:
    model_version: str
    scores: Mapping[str, LocalScoreResult]
    diagnostics: Mapping[str, ModelDiagnostics]
    predictions: tuple[HeadPrediction, ...]
    missing_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "scores", MappingProxyType(dict(self.scores)))
        object.__setattr__(self, "diagnostics", MappingProxyType(dict(self.diagnostics)))


class ProfileCombinerPort(Protocol):
    """Combine pre-risk head signals into one target prediction."""

    def combine(self, predictions: Sequence[HeadPrediction]) -> HeadPrediction: ...


@dataclass(frozen=True)
class ProfileEvidence:
    historical_status: Literal["historical_rejected", "historical_unavailable", "historical_validated"]
    historical_failure_reasons: tuple[str, ...]
    activation_basis: Literal["manual_user_override", "trained_artifact"]
    monitoring_mode: Literal["automatic_t1_outcome_settlement"] = "automatic_t1_outcome_settlement"
    automatic_model_update: bool = False
    loss_probability_status: Literal["not_modeled"] = "not_modeled"
    training_anchor: Literal["15:00_close"] = "15:00_close"
    runtime_anchor: Literal["14:50"] = "14:50"
    point_in_time_parity: bool = False


@dataclass(frozen=True)
class HeadRuntime:
    strategy: Strategy
    predictor: HeadPredictorPort


@dataclass(frozen=True)
class ProfileIdentity:
    profile_id: ScoringProfileId
    model_id: str
    model_hash: str


@dataclass(frozen=True)
class LoadedScoringProfile:
    identity: ProfileIdentity
    heads: tuple[HeadRuntime, ...]
    combiner: ProfileCombinerPort
    evidence: ProfileEvidence


@dataclass(frozen=True)
class ScoringProfileRuntimeStatus:
    active: bool
    profile_id: ScoringProfileId
    model_id: str
    model_hash: str
    scoring_version: str
    activation_basis: Literal["manual_user_override", "trained_artifact"]
    historical_status: Literal["historical_rejected", "historical_unavailable", "historical_validated"]
    historical_failure_reasons: tuple[str, ...]
    monitoring_mode: Literal["automatic_t1_outcome_settlement"]
    automatic_model_update: bool
    loss_probability_status: Literal["not_modeled"]
    training_anchor: Literal["15:00_close"] = "15:00_close"
    runtime_anchor: Literal["14:50"] = "14:50"
    point_in_time_parity: bool = False


class ScoringCapabilityPort(Protocol):
    @property
    def history_required_sessions(self) -> int: ...

    def is_input_eligible(self, feature: FeatureSnapshot) -> bool: ...

    def score(self, features: Sequence[FeatureSnapshot]) -> ModelScoreBatch: ...

    def status(self) -> ScoringProfileRuntimeStatus: ...


class ModelScoringPort(Protocol):
    def uses_model(self, strategy: Strategy) -> bool: ...

    def history_required_sessions(self, strategy: Strategy) -> int: ...

    def is_input_eligible(self, strategy: Strategy, feature: FeatureSnapshot) -> bool: ...

    def score(self, strategy: Strategy, features: Sequence[FeatureSnapshot]) -> ModelScoreBatch | None: ...

    def status(self) -> ScoringProfileRuntimeStatus | None: ...


__all__ = [
    "HeadPrediction",
    "HeadPredictorPort",
    "HeadRuntime",
    "LoadedScoringProfile",
    "ModelDiagnostics",
    "ModelScoreBatch",
    "ModelScoringPort",
    "ProfileCombinerPort",
    "ProfileEvidence",
    "ProfileIdentity",
    "ScoringHeadInput",
    "ScoringCapabilityPort",
    "ScoringProfileRuntimeStatus",
]
