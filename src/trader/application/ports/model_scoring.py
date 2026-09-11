"""Typed application boundary for strategy-aware model scoring."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Protocol

from trader.domain.market.models import FeatureSnapshot
from trader.domain.recommendation.model_scoring.profile_identity import ScoringProfileId
from trader.domain.recommendation.model_scoring.residualization import ExposureContract
from trader.domain.recommendation.models import Strategy


@dataclass(frozen=True)
class ModelInput:
    """Validated feature vector passed from an application scorer to a head."""

    code: str
    alpha_features: tuple[float, ...]
    industry: str = ""

    def __post_init__(self) -> None:
        if len(self.code) != 6 or not self.code.isdigit():
            raise ValueError("model input code is invalid")
        if not self.alpha_features or any(not math.isfinite(value) for value in self.alpha_features):
            raise ValueError("model input alpha features must be non-empty and finite")
        if not isinstance(self.industry, str):
            raise ValueError("model input industry must be text")


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

    def predict(self, inputs: Sequence[ModelInput]) -> tuple[HeadPrediction, ...]: ...


@dataclass(frozen=True)
class ModelPrediction:
    code: str
    predicted_excess_return: float
    model_disagreement: float

    def __post_init__(self) -> None:
        if len(self.code) != 6 or not self.code.isdigit():
            raise ValueError("model prediction code is invalid")
        if not math.isfinite(self.predicted_excess_return):
            raise ValueError("model prediction must be finite")
        if not math.isfinite(self.model_disagreement) or self.model_disagreement < 0.0:
            raise ValueError("model disagreement must be finite and non-negative")


class ModelPredictorPort(Protocol):
    @property
    def profile_id(self) -> ScoringProfileId: ...

    @property
    def model_id(self) -> str: ...

    @property
    def model_hash(self) -> str: ...

    @property
    def feature_ids(self) -> tuple[str, ...]: ...

    @property
    def exposure_contract(self) -> ExposureContract: ...

    @property
    def industry_ids(self) -> tuple[str, ...]: ...

    def predict(self, inputs: Sequence[ModelInput]) -> tuple[ModelPrediction, ...]: ...


@dataclass(frozen=True)
class ModelDiagnostics:
    signal_score: float
    predicted_excess_return_pct: float
    estimated_cost_pct: float
    predicted_net_excess_pct: float
    model_disagreement_pct: float

    def __post_init__(self) -> None:
        values = (
            self.signal_score,
            self.predicted_excess_return_pct,
            self.estimated_cost_pct,
            self.predicted_net_excess_pct,
            self.model_disagreement_pct,
        )
        if (
            any(not math.isfinite(value) for value in values)
            or not 0.0 <= self.signal_score <= 100.0
            or self.estimated_cost_pct < 0.0
            or self.model_disagreement_pct < 0.0
        ):
            raise ValueError("model diagnostics are invalid")


@dataclass(frozen=True)
class ModelScoreBatch:
    model_version: str
    diagnostics: Mapping[str, ModelDiagnostics]
    predictions: tuple[HeadPrediction, ...]
    missing_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        diagnostics = dict(self.diagnostics)
        prediction_codes = tuple(item.code for item in self.predictions)
        if (
            not self.model_version
            or len(prediction_codes) != len(set(prediction_codes))
            or set(diagnostics) != set(prediction_codes)
            or len(self.missing_codes) != len(set(self.missing_codes))
            or set(self.missing_codes).intersection(prediction_codes)
        ):
            raise ValueError("model score batch identity is invalid")
        object.__setattr__(self, "diagnostics", MappingProxyType(diagnostics))


@dataclass(frozen=True)
class ModelScoringContext:
    time_budget_seconds: float | None = None
    input_age_seconds: float = 0.0

    def __post_init__(self) -> None:
        if (
            (
                self.time_budget_seconds is not None
                and (not math.isfinite(self.time_budget_seconds) or self.time_budget_seconds < 0.0)
            )
            or not math.isfinite(self.input_age_seconds)
            or self.input_age_seconds < 0.0
        ):
            raise ValueError("model scoring context must contain finite non-negative timing values")


class ModelScoringDeadlineError(RuntimeError):
    """The current model batch can no longer finish before its scoring boundary."""


@dataclass(frozen=True)
class ModelComputationStageStatus:
    calculator_group: str
    execution_count: int
    last_duration_ms: float
    cumulative_duration_ms: float

    def __post_init__(self) -> None:
        if (
            not self.calculator_group
            or self.execution_count < 1
            or not math.isfinite(self.last_duration_ms)
            or not math.isfinite(self.cumulative_duration_ms)
            or self.last_duration_ms < 0.0
            or self.cumulative_duration_ms < self.last_duration_ms
        ):
            raise ValueError("model computation stage status is invalid")


@dataclass(frozen=True)
class ModelComputationStatus:
    candidate_count: int = 0
    request_count: int = 0
    cache_hit_count: int = 0
    predictor_batch_count: int = 0
    computed_groups: tuple[str, ...] = ()
    reused_groups: tuple[str, ...] = ()
    stage_durations: tuple[ModelComputationStageStatus, ...] = ()
    decision_age_ms: float = 0.0
    deadline_abandon_reason: str | None = None

    def __post_init__(self) -> None:
        counts = (self.candidate_count, self.request_count, self.cache_hit_count, self.predictor_batch_count)
        if (
            any(value < 0 for value in counts)
            or self.cache_hit_count > self.request_count
            or len(set(self.computed_groups)) != len(self.computed_groups)
            or len(set(self.reused_groups)) != len(self.reused_groups)
            or set(self.computed_groups).intersection(self.reused_groups)
            or not math.isfinite(self.decision_age_ms)
            or self.decision_age_ms < 0.0
            or self.deadline_abandon_reason == ""
        ):
            raise ValueError("model computation status is invalid")


class ProfileCombinerPort(Protocol):
    """Combine pre-risk head signals into one target prediction."""

    def combine(self, predictions: Sequence[HeadPrediction]) -> HeadPrediction: ...


@dataclass(frozen=True)
class ProfileEvidence:
    historical_status: Literal[
        "historical_rejected",
        "historical_unavailable",
        "historical_data_insufficient",
        "historical_validated",
    ]
    historical_failure_reasons: tuple[str, ...]
    activation_basis: Literal["manual_user_override", "trained_artifact"]
    monitoring_mode: Literal["automatic_t1_outcome_settlement"] = "automatic_t1_outcome_settlement"
    automatic_model_update: bool = False
    loss_probability_status: Literal["not_modeled"] = "not_modeled"
    training_anchor: Literal["15:00_close", "15:00_close_proxy", "14:50_point_in_time"] = "15:00_close"
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
    historical_status: Literal[
        "historical_rejected",
        "historical_unavailable",
        "historical_data_insufficient",
        "historical_validated",
    ]
    historical_failure_reasons: tuple[str, ...]
    monitoring_mode: Literal["automatic_t1_outcome_settlement"]
    automatic_model_update: bool
    loss_probability_status: Literal["not_modeled"]
    computation: ModelComputationStatus = ModelComputationStatus()
    training_anchor: Literal["15:00_close", "15:00_close_proxy", "14:50_point_in_time"] = "15:00_close"
    runtime_anchor: Literal["14:50"] = "14:50"
    point_in_time_parity: bool = False


class ScoringCapabilityPort(Protocol):
    @property
    def history_required_sessions(self) -> int: ...

    def is_input_eligible(self, feature: FeatureSnapshot) -> bool: ...

    def score(
        self,
        features: Sequence[FeatureSnapshot],
        *,
        context: ModelScoringContext | None = None,
    ) -> ModelScoreBatch: ...

    def status(self) -> ScoringProfileRuntimeStatus: ...


class ModelScoringPort(Protocol):
    def uses_model(self, strategy: Strategy) -> bool: ...

    def history_required_sessions(self, strategy: Strategy) -> int: ...

    def is_input_eligible(self, strategy: Strategy, feature: FeatureSnapshot) -> bool: ...

    def score(
        self,
        strategy: Strategy,
        features: Sequence[FeatureSnapshot],
        *,
        context: ModelScoringContext | None = None,
    ) -> ModelScoreBatch | None: ...

    def status(self) -> ScoringProfileRuntimeStatus | None: ...


__all__ = [
    "HeadPrediction",
    "HeadPredictorPort",
    "HeadRuntime",
    "LoadedScoringProfile",
    "ModelDiagnostics",
    "ModelComputationStageStatus",
    "ModelComputationStatus",
    "ModelInput",
    "ModelScoreBatch",
    "ModelPrediction",
    "ModelPredictorPort",
    "ModelScoringPort",
    "ModelScoringContext",
    "ModelScoringDeadlineError",
    "ProfileCombinerPort",
    "ProfileEvidence",
    "ProfileIdentity",
    "ScoringCapabilityPort",
    "ScoringProfileRuntimeStatus",
]
