"""Build candidate-recall attribution from an isolated point-in-time dataset."""

from __future__ import annotations

from datetime import date
from typing import Protocol

from trader.training.domain.evaluation.candidate_recall_ledger import (
    AttributeCandidateRecallDay,
    BuildCandidateRecallReport,
    CandidateRecallDayAttribution,
    CandidateRecallDayTrace,
    CandidateRecallInsufficientReport,
    CandidateRecallReport,
    CandidateRecallTraceMismatchError,
)
from trader.training.domain.evaluation.point_in_time_dataset import PointInTimeDatasetReport


class CandidateRecallTracePort(Protocol):
    def load_day_trace(self, trade_date: date, dataset_day_hash: str) -> CandidateRecallDayTrace | None: ...


class CandidateRecallLedgerBuilder:
    def __init__(self, trace_source: CandidateRecallTracePort) -> None:
        self._trace_source = trace_source

    def build(self, dataset: PointInTimeDatasetReport) -> CandidateRecallReport:
        if dataset.state != "historical_point_in_time_parity" or dataset.manifest is None:
            return CandidateRecallInsufficientReport(dataset, "point_in_time_dataset_unavailable")
        days: list[CandidateRecallDayAttribution] = []
        for day in dataset.days:
            trace = self._trace_source.load_day_trace(day.trade_date, day.content_hash)
            if trace is None:
                return CandidateRecallInsufficientReport(dataset, "candidate_recall_trace_unavailable")
            try:
                days.append(AttributeCandidateRecallDay(day, trace))
            except CandidateRecallTraceMismatchError:
                return CandidateRecallInsufficientReport(dataset, "candidate_recall_trace_invalid")
        try:
            return BuildCandidateRecallReport(dataset, tuple(days))
        except CandidateRecallTraceMismatchError:
            return CandidateRecallInsufficientReport(dataset, "candidate_recall_trace_invalid")


__all__ = ["CandidateRecallLedgerBuilder", "CandidateRecallTracePort"]
