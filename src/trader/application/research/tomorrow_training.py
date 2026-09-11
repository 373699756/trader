"""Tomorrow V3 training-date access control."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal, Protocol

from trader.domain.research.baostock_daily import BaoStockTrainingSplit

TomorrowTrainingStage = Literal[
    "resource_preflight",
    "partition_validation",
    "history_conversion",
    "cross_section_conversion",
    "model_fit",
    "artifact_publish",
    "completed",
]
TomorrowTrainingProgressState = Literal["started", "running", "completed"]
TOMORROW_TRAINING_COMPUTE_THREADS = 2
TOMORROW_TRAINING_PEAK_RSS_MIB = 2_048


@dataclass(frozen=True)
class TomorrowTrainingProgress:
    stage: TomorrowTrainingStage
    state: TomorrowTrainingProgressState
    completed_units: int
    total_units: int
    produced_units: int = 0

    def __post_init__(self) -> None:
        if (
            min(self.completed_units, self.total_units, self.produced_units) < 0
            or self.completed_units > self.total_units
        ):
            raise ValueError("Tomorrow V3 training progress counts are invalid")


class TomorrowTrainingProgressPort(Protocol):
    def publish(self, progress: TomorrowTrainingProgress) -> None: ...


@dataclass(frozen=True)
class TomorrowTrainingWindow:
    split: BaoStockTrainingSplit
    readable_dates: frozenset[date] = field(init=False)

    def __post_init__(self) -> None:
        readable = frozenset(
            (
                *self.split.development_dates,
                *self.split.first_embargo_dates,
                *self.split.confirmation_dates,
                *self.split.second_embargo_dates,
                *self.split.daily_proxy_holdout_dates,
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


__all__ = [
    "TOMORROW_TRAINING_COMPUTE_THREADS",
    "TOMORROW_TRAINING_PEAK_RSS_MIB",
    "TomorrowTrainingProgress",
    "TomorrowTrainingProgressPort",
    "TomorrowTrainingProgressState",
    "TomorrowTrainingStage",
    "TomorrowTrainingWindow",
]
