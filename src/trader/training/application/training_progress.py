"""Typed progress events for model training and terminal reporting."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol

from trader.recommendation.domain.publication.models import Strategy

TrainingStage = Literal[
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
    "event_model_fit",
    "model_calibration",
    "model_evaluation",
    "model_reuse",
    "final_model_refit",
    "artifact_publish",
]
TrainingProgressState = Literal["started", "running", "completed", "failed"]


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
class TrainingSampleProgress:
    completed_days: int
    total_days: int
    processed_rows: int
    warmup_rows: int = 0
    phase: Literal["warmup", "generation"] = "warmup"
    pending_samples: int = 0

    def __post_init__(self) -> None:
        if (
            any(type(value) is not int or value < 0 for value in (
                self.completed_days, self.total_days, self.processed_rows, self.warmup_rows, self.pending_samples,
            ))
            or self.completed_days > self.total_days or self.warmup_rows > self.processed_rows
            or self.phase not in {"warmup", "generation"}
        ):
            raise ValueError("training_sample_progress_invalid")


@dataclass(frozen=True)
class TrainingProgress:
    stage: TrainingStage
    state: TrainingProgressState
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
    sample_progress: TrainingSampleProgress | None = field(default=None, kw_only=True)
    input_progress: TrainingSampleProgress | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        if (
            min(self.completed_units, self.total_units, self.produced_units) < 0
            or self.completed_units > self.total_units
            or self.strategy is Strategy.LONG
            or type(self.total_is_exact) is not bool
            or self.count_unit not in {"tasks", "industries"}
            or self.validation is not None and not isinstance(self.validation, TrainingValidationProgress)
            or any(value is not None and not isinstance(value, TrainingSampleProgress)
                   for value in (self.sample_progress, self.input_progress))
            or self.sample_progress is not None and (
                self.stage != "history_conversion" or self.input_progress is not None
                or (self.completed_units, self.total_units)
                != (self.sample_progress.completed_days, self.sample_progress.total_days)
            )
            or self.input_progress is not None and self.stage != "input_read"
            or self.fit_counts is not None and (
                not isinstance(self.fit_counts, TrainingFitCounts)
                or (self.completed_units, self.total_units, self.produced_units)
                != (self.fit_counts.processed, self.fit_counts.total, self.fit_counts.fitted)
            )
        ):
            raise ValueError("training progress counts are invalid")


class TrainingProgressPort(Protocol):
    def publish(self, progress: TrainingProgress) -> None: ...


class CancellableTrainingProgress:
    def __init__(self, target: TrainingProgressPort, cancelled: Callable[[], bool]) -> None:
        self._target = target
        self._cancelled = cancelled

    def publish(self, progress: TrainingProgress) -> None:
        if self._cancelled():
            raise InterruptedError("profile training cancelled")
        self._target.publish(progress)


__all__ = [
    "CancellableTrainingProgress",
    "TrainingFitCounts",
    "TrainingFitDetail",
    "TrainingFitPhase",
    "TrainingProgress",
    "TrainingProgressPort",
    "TrainingProgressState",
    "TrainingSampleDiagnostics",
    "TrainingSampleProgress",
    "TrainingStage",
    "TrainingValidationProgress",
]
