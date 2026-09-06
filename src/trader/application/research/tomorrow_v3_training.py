"""Tomorrow V3 training-date access control."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal, Protocol

from trader.domain.research.baostock_daily import BaoStockV3Split

TomorrowV3TrainingStage = Literal["input_snapshot", "sample_build", "model_fit", "completed"]


@dataclass(frozen=True)
class TomorrowV3TrainingProgress:
    stage: TomorrowV3TrainingStage
    processed_codes: int
    total_codes: int

    def __post_init__(self) -> None:
        if min(self.processed_codes, self.total_codes) < 0 or self.processed_codes > self.total_codes:
            raise ValueError("Tomorrow V3 training progress counts are invalid")


class TomorrowV3TrainingProgressPort(Protocol):
    def publish(self, progress: TomorrowV3TrainingProgress) -> None: ...


@dataclass(frozen=True)
class TomorrowV3TrainingWindow:
    split: BaoStockV3Split
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
    "TomorrowV3TrainingProgress",
    "TomorrowV3TrainingProgressPort",
    "TomorrowV3TrainingStage",
    "TomorrowV3TrainingWindow",
]
