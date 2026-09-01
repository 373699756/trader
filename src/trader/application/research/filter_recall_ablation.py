"""Application wrapper for the offline filter recall ablation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from trader.domain.research.filter_recall_ablation import FilterAblationRow, FilterRecallAblationReport, run_filter_recall_ablation


@dataclass(frozen=True)
class FilterRecallAblationRequest:
    strategy: str
    development_dates: tuple[date, ...]
    rows: tuple[FilterAblationRow, ...]


def execute_filter_recall_ablation(request: FilterRecallAblationRequest) -> FilterRecallAblationReport:
    return run_filter_recall_ablation(request.rows, strategy=request.strategy, development_dates=request.development_dates)


__all__ = ["FilterRecallAblationRequest", "execute_filter_recall_ablation"]
