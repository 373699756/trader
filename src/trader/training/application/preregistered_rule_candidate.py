"""Application wrapper for preregistered rule candidates."""

from __future__ import annotations

from dataclasses import dataclass

from trader.training.domain.evaluation.filter_recall_ablation import FilterAblationRow, FilterRecallAblationReport
from trader.training.domain.evaluation.preregistered_rule_candidate import (
    EvaluateRuleCandidateFamily as evaluate_family,
)
from trader.training.domain.evaluation.preregistered_rule_candidate import (
    PreregisteredRuleCandidateFamily,
    PreregisterRuleCandidates,
    RuleCandidateMetrics,
)


@dataclass(frozen=True)
class RuleCandidateEvaluation:
    family: PreregisteredRuleCandidateFamily
    metrics: tuple[RuleCandidateMetrics, ...]


def BuildPreregisteredRuleCandidateFamily(report: FilterRecallAblationReport) -> PreregisteredRuleCandidateFamily:
    return PreregisterRuleCandidates(report)


def EvaluateRuleCandidateFamily(
    family: PreregisteredRuleCandidateFamily, rows: tuple[FilterAblationRow, ...]
) -> RuleCandidateEvaluation:
    report = evaluate_family(family, rows)
    return RuleCandidateEvaluation(family, report.metrics)


__all__ = [
    "RuleCandidateEvaluation",
    "BuildPreregisteredRuleCandidateFamily",
    "EvaluateRuleCandidateFamily",
]
