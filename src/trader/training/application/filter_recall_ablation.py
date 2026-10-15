"""Application wrapper for the offline filter recall ablation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from trader.training.domain.evaluation.filter_recall_ablation import (
    FilterAblationRow,
    FilterRecallAblationReport,
    RunFilterRecallAblation,
)


@dataclass(frozen=True)
class FilterRecallAblationRequest:
    strategy: str
    development_dates: tuple[date, ...]
    rows: tuple[FilterAblationRow, ...]


def ExecuteFilterRecallAblation(request: FilterRecallAblationRequest) -> FilterRecallAblationReport:
    return RunFilterRecallAblation(request.rows, strategy=request.strategy, development_dates=request.development_dates)


__all__ = ["FilterRecallAblationRequest", "ExecuteFilterRecallAblation"]
