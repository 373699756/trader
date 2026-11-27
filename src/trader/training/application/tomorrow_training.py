"""Tomorrow V3 training-date access control."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from trader.training.domain.production_diagnostic_dates import ProductionDiagnosticDatePlan
from trader.training.domain.training_dates import MAX_TRAINING_SESSIONS, TrainingDateSplit

TOMORROW_TRAINING_COMPUTE_THREADS = 2
TOMORROW_TRAINING_PEAK_RSS_MIB = 2_048


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
    "ProductionTrainingWindow",
    "TomorrowTrainingWindow",
    "TOMORROW_TRAINING_COMPUTE_THREADS",
    "TOMORROW_TRAINING_PEAK_RSS_MIB",
]
