"""Reports for the shared next-close probability training job."""

from dataclasses import dataclass

from trader.recommendation.domain.outcomes.prediction_target import PredictionTarget
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId


@dataclass(frozen=True, slots=True)
class TomorrowProbabilitySelection:
    rule_hits: int
    logistic_hits: int
    lightgbm_hits: int
    comparable_seats: int
    rule_unsettled: int
    logistic_unsettled: int
    lightgbm_unsettled: int


@dataclass(frozen=True, slots=True)
class TomorrowProbabilityTrainingResult:
    history_snapshot_hash: str
    model_identity: str | None
    algorithm: str | None
    selection: TomorrowProbabilitySelection | None = None
    assessment_rows: int = 0
    assessment_positive_rows: int = 0
    assessment_brier: float | None = None
    assessment_hits: int = 0
    assessment_seats: int = 0
    assessment_unsettled: int = 0
    pending_labels: int = 0
    missing_features: int = 0
    failure: str | None = None

    @property
    def ready(self) -> bool:
        return self.model_identity is not None and self.failure is None


@dataclass(frozen=True, slots=True)
class TomorrowProbabilityGroupResult:
    target: PredictionTarget
    profiles: tuple[ScoringProfileId, ...]
    result: TomorrowProbabilityTrainingResult
