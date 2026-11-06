"""Tomorrow V3 training-date access control."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal, Protocol

from trader.recommendation.domain.publication.models import Strategy
from trader.training.domain.training_dates import TrainingDateSplit

TomorrowTrainingStage = Literal[
    "resource_preflight",
    "input_read",
    "training_preparation",
    "history_conversion",
    "cross_section_conversion",
    "sample_index",
    "sample_cache",
    "sample_cache_check",
    "sample_cache_seal",
    "target_statistics",
    "industry_statistics",
    "model_fit",
    "final_model_refit",
    "artifact_publish",
]
TomorrowTrainingProgressState = Literal["started", "running", "completed"]
TOMORROW_TRAINING_COMPUTE_THREADS = 2
TOMORROW_TRAINING_PEAK_RSS_MIB = 2_048


@dataclass(frozen=True)
class TrainingSampleDiagnostics:
    missing_industry: int
    missing_prices: int
    short_window: int
    invalid_features: int
    missing_labels: int
    suspended_sessions: int = 0
    estimated_sessions: int = 0
    missing_t1: int = 0
    missing_d25: int = 0


TrainingFitPhase = Literal[
    "preparation", "batch_cache", "normalization", "training", "early_stopping", "calibration", "completed", "skipped"
]


@dataclass(frozen=True)
class TrainingFitDetail:
    industry: str
    training_rows: int
    phase: TrainingFitPhase
    epoch: int = 0
    total_epochs: int = 0
    completed_batches: int = 0
    total_batches: int = 0
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        if (
            not self.industry
            or min(self.training_rows, self.epoch, self.total_epochs, self.completed_batches, self.total_batches) < 0
            or self.epoch > self.total_epochs
            or self.completed_batches > self.total_batches
            or (self.phase == "skipped") != bool(self.failure_reason)
        ):
            raise ValueError("training fit detail is invalid")


@dataclass(frozen=True)
class TomorrowTrainingProgress:
    stage: TomorrowTrainingStage
    state: TomorrowTrainingProgressState
    completed_units: int
    total_units: int
    produced_units: int = 0
    strategy: Strategy | None = None
    sample_diagnostics: TrainingSampleDiagnostics | None = None
    fit_detail: TrainingFitDetail | None = None

    def __post_init__(self) -> None:
        if (
            min(self.completed_units, self.total_units, self.produced_units) < 0
            or self.completed_units > self.total_units
            or self.strategy is Strategy.LONG
        ):
            raise ValueError("Tomorrow V3 training progress counts are invalid")


class TomorrowTrainingProgressPort(Protocol):
    def publish(self, progress: TomorrowTrainingProgress) -> None: ...


@dataclass(frozen=True)
class TomorrowTrainingWindow:
    split: TrainingDateSplit
    readable_dates: frozenset[date] = field(init=False)

    def __post_init__(self) -> None:
        readable = frozenset(
            (
                *self.split.warmup_dates,
                *self.split.development_dates,
                *self.split.first_embargo_dates,
                *self.split.confirmation_dates,
                *self.split.second_embargo_dates,
                *self.split.daily_proxy_holdout_dates,
                *self.split.terminal_embargo_dates,
            )
        )
        if readable.intersection(self.split.point_in_time_holdout_dates):
            raise ValueError("Tomorrow V3 readable dates overlap the point-in-time holdout")
        object.__setattr__(self, "readable_dates", readable)

    def require_readable(self, dates: tuple[date, ...]) -> None:
        requested = frozenset(dates)
        if requested.intersection(self.split.point_in_time_holdout_dates):
            raise ValueError("Tomorrow V3 point-in-time holdout is not readable")
        if not requested.issubset(self.readable_dates):
            raise ValueError("Tomorrow V3 date is outside the frozen split")


@dataclass(frozen=True)
class ProductionTrainingWindow:
    """Read current source labels for final refit, without a sealed-research claim."""

    split: TrainingDateSplit
    source_dates: tuple[date, ...]
    readable_dates: frozenset[date] = field(init=False)

    def __post_init__(self) -> None:
        if (
            type(self.source_dates) is not tuple
            or not self.source_dates
            or any(type(day) is not date for day in self.source_dates)
            or self.source_dates != tuple(sorted(set(self.source_dates)))
            or not self.split.warmup_dates
        ):
            raise ValueError("production_training_source_dates_invalid")
        required = (
            *TomorrowTrainingWindow(self.split).readable_dates,
            *self.split.point_in_time_holdout_dates,
        )
        if not set(required).issubset(self.source_dates):
            raise ValueError("production_training_source_calendar_mismatch")
        start = self.source_dates.index(self.split.warmup_dates[0])
        object.__setattr__(self, "readable_dates", frozenset(self.source_dates[start:]))

    def require_readable(self, dates: tuple[date, ...]) -> None:
        if not frozenset(dates).issubset(self.readable_dates):
            raise ValueError("production_training_date_outside_source")


__all__ = [
    "TOMORROW_TRAINING_COMPUTE_THREADS",
    "TOMORROW_TRAINING_PEAK_RSS_MIB",
    "TomorrowTrainingProgress",
    "TomorrowTrainingProgressPort",
    "TomorrowTrainingProgressState",
    "TomorrowTrainingStage",
    "TomorrowTrainingWindow",
    "ProductionTrainingWindow",
    "TrainingFitDetail",
    "TrainingFitPhase",
]
