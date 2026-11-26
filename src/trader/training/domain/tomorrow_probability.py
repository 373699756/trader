"""Reports for independently fitted next-close probability models."""

from dataclasses import dataclass
from datetime import date
from math import isfinite
from typing import Literal

from trader.recommendation.domain.outcomes.prediction_target import PredictionTarget
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId


@dataclass(frozen=True, slots=True)
class TomorrowDiagnosticAssessment:
    model_identity: str
    fitting_dates: tuple[date, ...]
    early_stopping_dates: tuple[date, ...]
    calibration_dates: tuple[date, ...]
    assessment_dates: tuple[date, ...]
    rows: int
    positive_rows: int
    brier: float
    hits: int
    seats: int
    unsettled: int

    def __post_init__(self) -> None:
        days = self.fitting_dates + self.early_stopping_dates + self.calibration_dates + self.assessment_dates
        if (
            len(self.model_identity) != 64 or any(c not in "0123456789abcdef" for c in self.model_identity)
            or not self.fitting_dates or not self.calibration_dates or not self.assessment_dates
            or days != tuple(sorted(set(days)))
            or any(type(n) is not int for n in (self.rows, self.positive_rows, self.hits, self.seats, self.unsettled))
            or self.rows < 1 or not 0 <= self.positive_rows <= self.rows or not isfinite(self.brier) or not 0 <= self.brier <= 1
            or min(self.hits, self.unsettled) < 0 or self.hits + self.unsettled > self.seats
        ):
            raise ValueError("tomorrow_diagnostic_assessment_invalid")


TomorrowWindow = Literal["source_calendar", "fitting", "early_stopping", "calibration", "assessment"]


@dataclass(frozen=True, slots=True)
class TomorrowWindowShortfall:
    partition: TomorrowWindow
    required_days: int
    available_days: int


@dataclass(frozen=True, slots=True)
class TomorrowProbabilityTrainingResult:
    history_snapshot_hash: str
    model_identity: str | None
    algorithm: str | None
    pending_labels: int = 0
    missing_features: int = 0
    failure: str | None = None
    diagnostic: TomorrowDiagnosticAssessment | None = None
    fitting_dates: tuple[date, ...] = ()
    early_stopping_dates: tuple[date, ...] = ()
    calibration_dates: tuple[date, ...] = ()
    window_shortfall: TomorrowWindowShortfall | None = None
    reused: bool = False

    @property
    def ready(self) -> bool:
        return self.model_identity is not None and self.failure is None

    @property
    def assessment_rows(self) -> int:
        return self.diagnostic.rows if self.diagnostic is not None else 0

    @property
    def assessment_positive_rows(self) -> int:
        return self.diagnostic.positive_rows if self.diagnostic is not None else 0

    @property
    def assessment_brier(self) -> float | None:
        return self.diagnostic.brier if self.diagnostic is not None else None

    @property
    def assessment_hits(self) -> int:
        return self.diagnostic.hits if self.diagnostic is not None else 0

    @property
    def assessment_seats(self) -> int:
        return self.diagnostic.seats if self.diagnostic is not None else 0

    @property
    def assessment_unsettled(self) -> int:
        return self.diagnostic.unsettled if self.diagnostic is not None else 0


@dataclass(frozen=True, slots=True)
class TomorrowProbabilityProfileResult:
    target: PredictionTarget
    profile: ScoringProfileId
    result: TomorrowProbabilityTrainingResult
