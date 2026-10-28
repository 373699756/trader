"""Typed command status for the zero-argument historical-data workflow."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

HistoryMaintenanceState = Literal[
    "blocked",
    "already_current",
    "already_running",
    "cancelled",
    "completed",
    "failed",
]


@dataclass(frozen=True)
class HistoryPriceGapReason:
    reason: str
    cells: int


@dataclass(frozen=True)
class HistoryPriceGapSummary:
    stocks: int
    cells: int
    first_date: date | None
    last_date: date | None
    reasons: tuple[HistoryPriceGapReason, ...]


@dataclass(frozen=True)
class HistoryMaintenanceStatus:
    state: HistoryMaintenanceState
    reason: str | None
    history_root: Path
    selected_baseline_source: str
    efficient_daily_source: str | None
    snapshot_hash: str | None
    data_cutoff: date | None
    label_cutoff: date | None
    unresolved_price_cells: int = 0
    price_gap_summary: HistoryPriceGapSummary | None = None

    def __post_init__(self) -> None:
        if self.unresolved_price_cells < 0:
            raise ValueError("unresolved history price cells must not be negative")

    @property
    def published(self) -> bool:
        return self.state in {"completed", "already_current"} and self.snapshot_hash is not None

    @property
    def needs_retry(self) -> bool:
        return not self.published or self.unresolved_price_cells > 0


__all__ = [
    "HistoryMaintenanceState",
    "HistoryMaintenanceStatus",
    "HistoryPriceGapReason",
    "HistoryPriceGapSummary",
]
