"""Orchestrate one offline point-in-time limited factor-family evaluation."""

from __future__ import annotations

from typing import Protocol

from trader.domain.research.candidate_recall_ledger import CandidateRecallReport
from trader.domain.research.limited_factor_family import (
    FactorFamilyCandidateSeries,
    LimitedFactorFamilyReport,
    LimitedFactorFamilySpec,
    evaluate_limited_factor_family,
    insufficient_limited_factor_family_report,
)
from trader.domain.research.point_in_time_dataset import PointInTimeDatasetReport


class LimitedFactorEvidencePort(Protocol):
    def load_candidate_series(
        self,
        spec: LimitedFactorFamilySpec,
        dataset: PointInTimeDatasetReport,
        recall: CandidateRecallReport,
    ) -> tuple[FactorFamilyCandidateSeries, ...] | None: ...


class LimitedFactorFamilyResearchBuilder:
    def __init__(self, evidence_source: LimitedFactorEvidencePort) -> None:
        self._evidence_source = evidence_source

    def build(
        self,
        spec: LimitedFactorFamilySpec,
        dataset: PointInTimeDatasetReport,
        recall: CandidateRecallReport,
    ) -> LimitedFactorFamilyReport:
        if recall.dataset_report_hash != dataset.content_hash:
            return _insufficient(spec, dataset, recall, "candidate_recall_parent_mismatch")
        if dataset.state != "historical_point_in_time_parity" or dataset.manifest is None:
            return _insufficient(spec, dataset, recall, "point_in_time_dataset_unavailable")
        if (
            recall.state != "candidate_recall_attributed"
            or recall.dataset_manifest_hash != dataset.manifest.content_hash
        ):
            return _insufficient(spec, dataset, recall, "candidate_recall_unavailable")
        series = self._evidence_source.load_candidate_series(spec, dataset, recall)
        if series is None:
            return _insufficient(spec, dataset, recall, "factor_family_evidence_unavailable")
        try:
            return evaluate_limited_factor_family(
                spec,
                dataset_report_hash=dataset.content_hash,
                dataset_manifest_hash=dataset.manifest.content_hash,
                recall_report_hash=recall.content_hash,
                series=series,
            )
        except ValueError:
            return _insufficient(spec, dataset, recall, "factor_family_evidence_invalid")


def _insufficient(
    spec: LimitedFactorFamilySpec,
    dataset: PointInTimeDatasetReport,
    recall: CandidateRecallReport,
    reason: str,
) -> LimitedFactorFamilyReport:
    return insufficient_limited_factor_family_report(
        spec,
        dataset_report_hash=dataset.content_hash,
        dataset_manifest_hash=dataset.manifest.content_hash if dataset.manifest is not None else None,
        recall_report_hash=recall.content_hash,
        reason=reason,
    )


__all__ = ["LimitedFactorEvidencePort", "LimitedFactorFamilyResearchBuilder"]
