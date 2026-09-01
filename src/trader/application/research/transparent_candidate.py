"""Application wrapper for preregistered transparent candidates."""

from __future__ import annotations

from dataclasses import dataclass

from trader.domain.research.filter_recall_ablation import FilterAblationRow, FilterRecallAblationReport
from trader.domain.research.transparent_candidate import (
    TransparentCandidate,
    TransparentCandidateFamily,
    TransparentCandidateMetrics,
    evaluate_transparent_candidate,
    preregister_transparent_candidates,
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
    metrics = tuple(evaluate_transparent_candidate(candidate, rows) for candidate in family.candidates)
    return TransparentCandidateEvaluation(family, metrics)


__all__ = ["TransparentCandidateEvaluation", "build_transparent_candidate_family", "evaluate_transparent_candidate_family"]
