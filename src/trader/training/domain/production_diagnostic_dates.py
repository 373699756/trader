"""Recent production diagnostics, independent of sealed research date partitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.training.domain.training_dates import MAX_TRAINING_SESSIONS, TrainingDateSplit

PRODUCTION_DIAGNOSTIC_CONTRACT = "recent_production_diagnostic_common_t5"


@dataclass(frozen=True)
class ProductionDiagnosticDatePlan:
    source_cutoff: date
    maturity_sessions: int
    fitting_dates: tuple[date, ...]
    early_stopping_dates: tuple[date, ...]
    calibration_dates: tuple[date, ...]
    validation_dates: tuple[date, ...]
    warmup_dates: tuple[date, ...]
    embargo_dates: tuple[tuple[date, ...], ...]
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        partitions = (
            self.fitting_dates, self.early_stopping_dates, self.calibration_dates,
            self.validation_dates, self.warmup_dates,
        )
        if (
            type(self.source_cutoff) is not date
            or type(self.maturity_sessions) is not int
            or self.maturity_sessions != 5
            or any(type(days) is not tuple or any(type(day) is not date for day in days) for days in partitions)
            or type(self.embargo_dates) is not tuple
            or any(type(days) is not tuple or len(days) != 5
                   or any(type(day) is not date for day in days) for days in self.embargo_dates)
            or not 1 <= len(self.fitting_dates) <= MAX_TRAINING_SESSIONS
            or len(self.early_stopping_dates) not in {0, 20}
            or len(self.calibration_dates) != 20
            or len(self.validation_dates) != 20
            or len(self.embargo_dates) != (3 if self.early_stopping_dates else 2)
        ):
            raise ValueError("production_diagnostic_date_plan_invalid")
        groups = (
            self.warmup_dates,
            self.fitting_dates,
            self.embargo_dates[0],
            *((self.early_stopping_dates, self.embargo_dates[1]) if self.early_stopping_dates else ()),
            self.calibration_dates,
            self.embargo_dates[-1],
            self.validation_dates,
        )
        ordered = tuple(day for days in groups for day in days)
        if (
            len(ordered) > MAX_TRAINING_SESSIONS
            or ordered != tuple(sorted(set(ordered)))
            or (self.source_cutoff - self.validation_dates[-1]).days < self.maturity_sessions
        ):
            raise ValueError("production_diagnostic_date_plan_invalid")
        object.__setattr__(self, "content_hash", CanonicalDigest((PRODUCTION_DIAGNOSTIC_CONTRACT, self)))


def BuildProductionDiagnosticDatePlan(
    calendar: tuple[date, ...],
    *,
    source_cutoff: date,
    fit_sessions: int,
    early_stopping_sessions: int,
    history_sessions: int,
    history_excludes_signal: bool = False,
) -> ProductionDiagnosticDatePlan:
    """Use a common T+5 mature diagnostic end for both heads and reserve only recent evaluation dates."""
    if (
        type(calendar) is not tuple
        or not calendar
        or len(calendar) > MAX_TRAINING_SESSIONS
        or any(type(day) is not date for day in calendar)
        or calendar != tuple(sorted(set(calendar)))
        or type(source_cutoff) is not date
        or source_cutoff not in calendar
        or type(fit_sessions) is not int or fit_sessions < 1
        or type(early_stopping_sessions) is not int or early_stopping_sessions not in {0, 20}
        or type(history_sessions) is not int or history_sessions < 1
        or type(history_excludes_signal) is not bool
    ):
        raise ValueError("production_diagnostic_calendar_or_settings_invalid")
    warmup_sessions = history_sessions if history_excludes_signal else history_sessions - 1
    cursor = calendar.index(source_cutoff) + 1 - 5

    def take(count: int) -> tuple[date, ...]:
        nonlocal cursor
        if cursor < count:
            raise ValueError("production_diagnostic_calendar_insufficient")
        cursor -= count
        return calendar[cursor:cursor + count]

    validation = take(20)
    validation_embargo = take(5)
    calibration = take(20)
    calibration_embargo = take(5) if early_stopping_sessions else ()
    early_stopping = take(early_stopping_sessions)
    fitting_embargo = take(5)
    fitting = take(fit_sessions)
    warmup = take(warmup_sessions)
    embargoes = (fitting_embargo, *((calibration_embargo,) if early_stopping_sessions else ()), validation_embargo)
    return ProductionDiagnosticDatePlan(
        source_cutoff, 5, fitting, early_stopping, calibration, validation, warmup, embargoes,
    )


@dataclass(frozen=True)
class TrainingSamplePartitions:
    training: tuple[date, ...]
    early_stopping: tuple[date, ...]
    calibration: tuple[date, ...]
    validation: tuple[date, ...]
    holdout: tuple[date, ...]


def TrainingSampleDatePartitions(
    split: TrainingDateSplit | ProductionDiagnosticDatePlan,
) -> TrainingSamplePartitions:
    if isinstance(split, ProductionDiagnosticDatePlan):
        return TrainingSamplePartitions(
            split.fitting_dates, split.early_stopping_dates, split.calibration_dates, split.validation_dates, (),
        )
    if isinstance(split, TrainingDateSplit):
        return TrainingSamplePartitions(
            split.model_fit_dates, split.early_stopping_dates, split.calibration_dates,
            split.confirmation_dates, split.daily_proxy_holdout_dates,
        )
    raise TypeError("training_sample_partition_plan_invalid")


__all__ = [
    "PRODUCTION_DIAGNOSTIC_CONTRACT",
    "ProductionDiagnosticDatePlan",
    "BuildProductionDiagnosticDatePlan",
    "TrainingSamplePartitions",
    "TrainingSampleDatePartitions",
]
