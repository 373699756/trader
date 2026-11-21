"""Reports for independently fitted next-close probability models."""

from dataclasses import dataclass

from trader.recommendation.domain.outcomes.prediction_target import PredictionTarget
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId


@dataclass(frozen=True, slots=True)
class TomorrowProbabilityTrainingResult:
    history_snapshot_hash: str
    model_identity: str | None
    algorithm: str | None
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
class TomorrowProbabilityProfileResult:
    target: PredictionTarget
    profile: ScoringProfileId
    result: TomorrowProbabilityTrainingResult
