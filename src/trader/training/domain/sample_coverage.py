"""Immutable sample-date coverage, independent of SQLite and fitting libraries."""

from dataclasses import dataclass
from datetime import date
from typing import Literal

TrainingSplitName = Literal["training", "early_stopping", "calibration", "validation", "holdout"]


@dataclass(frozen=True)
class SampleDateGap:
    trade_date: date
    sample_rows: int
    candidates: int
    missing_future_returns: tuple[int, ...]


@dataclass(frozen=True)
class SampleCoverage:
    target: str
    partition: TrainingSplitName
    expected_dates: int
    effective_dates: int
    sample_rows: int
    target_rows: int
    missing: tuple[SampleDateGap, ...]


__all__ = [
    "SampleCoverage",
    "SampleDateGap",
    "TrainingSplitName",
]
