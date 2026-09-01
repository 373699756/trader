"""Pure temporal contracts for Tomorrow daily-close challenger research."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

_EMBARGO_DAYS = 5
_WALK_FORWARD_FOLDS = 5
_MINIMUM_SPLIT_DATES = 30
_MINIMUM_POINT_IN_TIME_RESERVE_DAYS = 200


@dataclass(frozen=True)
class DailyCloseTemporalSplit:
    development_dates: tuple[date, ...]
    first_embargo_dates: tuple[date, ...]
    confirmation_dates: tuple[date, ...]
    second_embargo_dates: tuple[date, ...]
    terminal_holdout_dates: tuple[date, ...]
    point_in_time_reserved_dates: tuple[date, ...]
    embargo_days_per_boundary: int = _EMBARGO_DAYS
    minimum_point_in_time_reserve_days: int = _MINIMUM_POINT_IN_TIME_RESERVE_DAYS

    def __post_init__(self) -> None:
        groups = (
            self.development_dates,
            self.first_embargo_dates,
            self.confirmation_dates,
            self.second_embargo_dates,
            self.terminal_holdout_dates,
            self.point_in_time_reserved_dates,
        )
        if any(not group for group in groups):
            raise ValueError("daily-close temporal split groups must be non-empty")
        if self.embargo_days_per_boundary != _EMBARGO_DAYS:
            raise ValueError("daily-close temporal split requires two five-day embargoes")
        if len(self.first_embargo_dates) != _EMBARGO_DAYS or len(self.second_embargo_dates) != _EMBARGO_DAYS:
            raise ValueError("daily-close temporal split requires two five-day embargoes")
        if (
            self.minimum_point_in_time_reserve_days < _MINIMUM_POINT_IN_TIME_RESERVE_DAYS
            or len(self.point_in_time_reserved_dates) < self.minimum_point_in_time_reserve_days
        ):
            raise ValueError("daily-close split must reserve at least 200 dates for point-in-time holdout")
        _strictly_increasing(self.all_dates)

    @property
    def all_dates(self) -> tuple[date, ...]:
        return (
            self.development_dates
            + self.first_embargo_dates
            + self.confirmation_dates
            + self.second_embargo_dates
            + self.terminal_holdout_dates
            + self.point_in_time_reserved_dates
        )

    @property
    def daily_close_dates(self) -> tuple[date, ...]:
        return (
            self.development_dates
            + self.first_embargo_dates
            + self.confirmation_dates
            + self.second_embargo_dates
            + self.terminal_holdout_dates
        )


@dataclass(frozen=True)
class ExpandingWalkForwardFold:
    index: int
    training_dates: tuple[date, ...]
    validation_dates: tuple[date, ...]

    def __post_init__(self) -> None:
        if not 1 <= self.index <= _WALK_FORWARD_FOLDS:
            raise ValueError("daily-close walk-forward fold index is invalid")
        if not self.training_dates or not self.validation_dates:
            raise ValueError("daily-close walk-forward fold groups must be non-empty")
        _strictly_increasing(self.training_dates)
        _strictly_increasing(self.validation_dates)
        if self.training_dates[-1] >= self.validation_dates[0]:
            raise ValueError("daily-close walk-forward training must precede validation")


def split_complete_trading_dates(
    trading_dates: tuple[date, ...],
    *,
    point_in_time_reserve_days: int = _MINIMUM_POINT_IN_TIME_RESERVE_DAYS,
) -> DailyCloseTemporalSplit:
    """Reserve strict point-in-time dates, then split the earlier daily-close calendar."""

    _strictly_increasing(trading_dates)
    if point_in_time_reserve_days < _MINIMUM_POINT_IN_TIME_RESERVE_DAYS:
        raise ValueError("daily-close split must reserve at least 200 point-in-time trading dates")
    required_dates = point_in_time_reserve_days + _MINIMUM_SPLIT_DATES
    if len(trading_dates) < required_dates:
        raise ValueError(
            f"daily-close temporal split requires at least {required_dates} trading dates including the reserve"
        )

    daily_close_dates = trading_dates[:-point_in_time_reserve_days]
    point_in_time_reserved_dates = trading_dates[-point_in_time_reserve_days:]
    development_boundary = len(daily_close_dates) * 60 // 100
    terminal_boundary = len(daily_close_dates) * 80 // 100
    development_end = development_boundary - _EMBARGO_DAYS
    confirmation_end = terminal_boundary - _EMBARGO_DAYS
    if development_end < _WALK_FORWARD_FOLDS + 1 or confirmation_end <= development_boundary:
        raise ValueError("daily-close temporal split has insufficient dates around embargoes")

    return DailyCloseTemporalSplit(
        development_dates=daily_close_dates[:development_end],
        first_embargo_dates=daily_close_dates[development_end:development_boundary],
        confirmation_dates=daily_close_dates[development_boundary:confirmation_end],
        second_embargo_dates=daily_close_dates[confirmation_end:terminal_boundary],
        terminal_holdout_dates=daily_close_dates[terminal_boundary:],
        point_in_time_reserved_dates=point_in_time_reserved_dates,
        minimum_point_in_time_reserve_days=point_in_time_reserve_days,
    )


def build_expanding_walk_forward(
    development_dates: tuple[date, ...],
) -> tuple[ExpandingWalkForwardFold, ...]:
    """Build five deterministic expanding folds from six contiguous date blocks."""

    _strictly_increasing(development_dates)
    block_count = _WALK_FORWARD_FOLDS + 1
    if len(development_dates) < block_count:
        raise ValueError("daily-close walk-forward requires at least six development dates")

    quotient, remainder = divmod(len(development_dates), block_count)
    blocks: list[tuple[date, ...]] = []
    offset = 0
    for index in range(block_count):
        size = quotient + (1 if index < remainder else 0)
        blocks.append(development_dates[offset : offset + size])
        offset += size

    return tuple(
        ExpandingWalkForwardFold(
            index=index,
            training_dates=tuple(day for block in blocks[:index] for day in block),
            validation_dates=blocks[index],
        )
        for index in range(1, block_count)
    )


def _strictly_increasing(values: tuple[date, ...]) -> None:
    if not values or any(left >= right for left, right in zip(values, values[1:], strict=False)):
        raise ValueError("daily-close trading dates must be strictly increasing")


__all__ = [
    "DailyCloseTemporalSplit",
    "ExpandingWalkForwardFold",
    "build_expanding_walk_forward",
    "split_complete_trading_dates",
]
