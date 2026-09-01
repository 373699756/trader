"""Application wrapper for preregistered transparent candidates."""

from __future__ import annotations

from dataclasses import dataclass

from trader.domain.research.filter_recall_ablation import FilterAblationRow, FilterRecallAblationReport
from trader.domain.research.transparent_candidate import (
    TransparentCandidateFamily,
    TransparentCandidateMetrics,
    preregister_transparent_candidates,
)
from trader.domain.research.transparent_candidate import (
    evaluate_transparent_candidate_family as evaluate_family,
)


@dataclass(frozen=True)
class TransparentCandidateEvaluation:
    family: TransparentCandidateFamily
    metrics: tuple[TransparentCandidateMetrics, ...]


def build_transparent_candidate_family(report: FilterRecallAblationReport) -> TransparentCandidateFamily:
    return preregister_transparent_candidates(report)


def evaluate_transparent_candidate_family(
    family: TransparentCandidateFamily, rows: tuple[FilterAblationRow, ...]
) -> TransparentCandidateEvaluation:
    report = evaluate_family(family, rows)
    return TransparentCandidateEvaluation(family, report.metrics)


__all__ = [
    "TransparentCandidateEvaluation",
    "build_transparent_candidate_family",
    "evaluate_transparent_candidate_family",
]
