"""Typed command status for the zero-argument historical-data workflow."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

from trader.download.domain.history_control import HistoryTrainingDueReason
from trader.training.domain.model_publication import HeadTrainingStatus

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
    archive_root: Path
    selected_baseline_source: str
    efficient_daily_source: str | None
    active_snapshot_hash: str | None
    data_cutoff: date | None
    label_cutoff: date | None
    matured_label_days_since_training: int
    training_due: bool
    training_due_reason: HistoryTrainingDueReason
    automatic_training: bool
    unresolved_price_cells: int = 0
    price_gap_summary: HistoryPriceGapSummary | None = None
    training_heads: tuple[HeadTrainingStatus, ...] = ()

    def __post_init__(self) -> None:
        if self.matured_label_days_since_training < 0 or self.unresolved_price_cells < 0:
            raise ValueError("matured label days must not be negative")
        due_reasons = {"initial_training_required", "cadence_due", "input_revision_due", "training_contract_due"}
        if self.training_due != (self.training_due_reason in due_reasons):
            raise ValueError("training due flag and reason disagree")


__all__ = [
    "HistoryMaintenanceState",
    "HistoryMaintenanceStatus",
    "HistoryPriceGapReason",
    "HistoryPriceGapSummary",
    "HistoryTrainingDueReason",
]
