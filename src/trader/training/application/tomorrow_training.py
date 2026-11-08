"""Tomorrow V3 training-date access control."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal, Protocol

from trader.recommendation.domain.publication.models import Strategy
from trader.training.domain.production_diagnostic_dates import ProductionDiagnosticDatePlan
from trader.training.domain.training_dates import MAX_TRAINING_SESSIONS, TrainingDateSplit

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
    industry_name: str | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        if (
            not self.industry
            or min(self.training_rows, self.epoch, self.total_epochs, self.completed_batches, self.total_batches) < 0
            or self.epoch > self.total_epochs
            or self.completed_batches > self.total_batches
            or (self.phase == "skipped") != bool(self.failure_reason)
            or self.industry_name is not None and (
                not isinstance(self.industry_name, str)
                or not self.industry_name.strip() or self.industry_name != self.industry_name.strip()
            )
        ):
            raise ValueError("training fit detail is invalid")


@dataclass(frozen=True)
class TrainingFitCounts:
    processed: int
    total: int
    fitted: int
    skipped: int

    def __post_init__(self) -> None:
        values = (self.processed, self.total, self.fitted, self.skipped)
        if (
            any(type(value) is not int or value < 0 for value in values)
            or self.processed > self.total
            or self.processed != self.fitted + self.skipped
        ):
            raise ValueError("training_fit_counts_invalid")


@dataclass(frozen=True)
class TrainingValidationProgress:
    completed_rows: int
    total_rows: int

    def __post_init__(self) -> None:
        if (
            type(self.completed_rows) is not int or type(self.total_rows) is not int
            or not 0 <= self.completed_rows <= self.total_rows
        ):
            raise ValueError("training_validation_progress_invalid")


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
    total_is_exact: bool = field(default=True, kw_only=True)
    count_unit: Literal["tasks", "industries"] = field(default="tasks", kw_only=True)
    validation: TrainingValidationProgress | None = field(default=None, kw_only=True)
    fit_counts: TrainingFitCounts | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        if (
            min(self.completed_units, self.total_units, self.produced_units) < 0
            or self.completed_units > self.total_units
            or self.strategy is Strategy.LONG
            or type(self.total_is_exact) is not bool
            or self.count_unit not in {"tasks", "industries"}
            or self.validation is not None and not isinstance(self.validation, TrainingValidationProgress)
            or self.fit_counts is not None and (
                not isinstance(self.fit_counts, TrainingFitCounts)
                or (self.completed_units, self.total_units, self.produced_units)
                != (self.fit_counts.processed, self.fit_counts.total, self.fit_counts.fitted)
            )
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
    """Read recent production diagnostics and mature final labels through the frozen source cutoff."""

    split: ProductionDiagnosticDatePlan
    source_dates: tuple[date, ...]
    readable_dates: frozenset[date] = field(init=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.split, ProductionDiagnosticDatePlan)
            or type(self.source_dates) is not tuple
            or not self.source_dates
            or len(self.source_dates) > MAX_TRAINING_SESSIONS
            or any(type(day) is not date for day in self.source_dates)
            or self.source_dates != tuple(sorted(set(self.source_dates)))
        ):
            raise ValueError("production_training_source_dates_invalid")
        groups = (
            self.split.warmup_dates,
            self.split.fitting_dates,
            self.split.embargo_dates[0],
            *((self.split.early_stopping_dates, self.split.embargo_dates[1]) if self.split.early_stopping_dates else ()),
            self.split.calibration_dates,
            self.split.embargo_dates[-1],
            self.split.validation_dates,
        )
        required = tuple(day for days in groups for day in days)
        if self.split.source_cutoff not in self.source_dates or not set(required).issubset(self.source_dates):
            raise ValueError("production_training_source_calendar_mismatch")
        start = self.source_dates.index(required[0])
        end = self.source_dates.index(self.split.validation_dates[-1]) + 1
        source_end = self.source_dates.index(self.split.source_cutoff) + 1
        if required != self.source_dates[start:end] or source_end - end != self.split.maturity_sessions:
            raise ValueError("production_training_source_calendar_mismatch")
        object.__setattr__(self, "readable_dates", frozenset(self.source_dates[start:source_end]))

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
    "TrainingFitCounts",
    "TrainingFitPhase",
    "TrainingValidationProgress",
]
