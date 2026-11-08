"""Per-head signal dates proven possible by metadata or present in sealed samples."""

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import date
from typing import Literal

from trader.recommendation.domain.publication.models import Strategy

TrainingDatePartition = Literal["fitting", "early_stopping", "calibration", "validation", "final_fitting"]
TrainingDateBasis = Literal["metadata_upper_bound", "sample_index"]


@dataclass(frozen=True)
class TrainingDateCoverage:
    strategy: Strategy
    partition: TrainingDatePartition
    basis: TrainingDateBasis
    source_cutoff: date
    label_cutoff: date
    required_dates: tuple[date, ...]
    available_dates: tuple[date, ...]

    def __post_init__(self) -> None:
        if (
            self.partition not in {"fitting", "early_stopping", "calibration", "validation", "final_fitting"}
            or self.basis not in {"metadata_upper_bound", "sample_index"}
            or not isinstance(self.strategy, Strategy)
            or self.label_cutoff >= self.source_cutoff
            or self.required_dates != tuple(sorted(set(self.required_dates)))
            or self.available_dates != tuple(sorted(set(self.available_dates)))
            or not set(self.available_dates) <= set(self.required_dates)
            or any(day > self.label_cutoff for day in self.required_dates)
        ):
            raise ValueError("training_date_coverage_invalid")

    @property
    def expected_dates(self) -> int:
        return len(self.required_dates)

    @property
    def effective_dates(self) -> int:
        return len(self.available_dates)

    @property
    def missing_dates(self) -> tuple[date, ...]:
        available = frozenset(self.available_dates)
        return tuple(day for day in self.required_dates if day not in available)


@dataclass(frozen=True)
class TrainingDateRequirement:
    strategy: Strategy
    partition: TrainingDatePartition
    source_cutoff: date
    label_cutoff: date
    required_dates: tuple[date, ...]

    def observe(self, available: tuple[date, ...], basis: TrainingDateBasis) -> TrainingDateCoverage:
        present = frozenset(available)
        return TrainingDateCoverage(
            self.strategy, self.partition, basis, self.source_cutoff, self.label_cutoff,
            self.required_dates, tuple(day for day in self.required_dates if day in present),
        )


def TrainingDateCoverageMatches(
    requirements: tuple[TrainingDateRequirement, ...], coverage: tuple[TrainingDateCoverage, ...], *, basis: TrainingDateBasis,
) -> bool:
    return (
        len(coverage) == len(requirements)
        and len({(item.strategy, item.partition) for item in coverage}) == len(requirements)
        and all(item.basis == basis and any(
            item.strategy is expected.strategy and item.partition == expected.partition
            and item.source_cutoff == expected.source_cutoff and item.label_cutoff == expected.label_cutoff
            and item.required_dates == expected.required_dates for expected in requirements
        ) for item in coverage)
    )


def PossibleTrainingSignalDates(
    calendar: tuple[date, ...], source_cutoff: date, classified_codes: tuple[str, ...],
    listing_intervals: tuple[tuple[str, date, date | None], ...], *, history_sessions: int,
    history_excludes_signal: bool, maturity_sessions: int,
) -> tuple[date, ...]:
    """Optimistic union across stocks; prices, volume and features still need exact checks.

    An unknown listing interval imposes no extra restriction. It cannot prove a
    date impossible; production snapshots provide the actual listing intervals.
    """
    if (
        not calendar or calendar != tuple(sorted(set(calendar))) or source_cutoff not in calendar
        or history_sessions < 1 or maturity_sessions not in {1, 5}
    ):
        raise ValueError("training_signal_date_metadata_invalid")
    cutoff = bisect_right(calendar, source_cutoff)
    warmup = history_sessions if history_excludes_signal else history_sessions - 1
    lifetimes = tuple(sorted(listing_intervals))
    codes = tuple(item[0] for item in lifetimes)
    spans = []
    for code in classified_codes:
        position = bisect_left(codes, code)
        lifetime = lifetimes[position] if position < len(codes) and codes[position] == code else None
        listed = bisect_left(calendar, lifetime[1]) if lifetime is not None else 0
        delisted = bisect_left(calendar, lifetime[2]) if lifetime is not None and lifetime[2] is not None else cutoff
        start, end = listed + warmup, min(cutoff, delisted) - maturity_sessions
        if start < end:
            spans.append((start, end))
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return tuple(calendar[position] for start, end in merged for position in range(start, end))


__all__ = [
    "PossibleTrainingSignalDates", "TrainingDateBasis", "TrainingDateCoverage", "TrainingDatePartition", "TrainingDateRequirement",
    "TrainingDateCoverageMatches",
]
