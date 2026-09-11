"""Application wrapper for preregistered rule candidates."""

from __future__ import annotations

from dataclasses import dataclass

from trader.domain.research.filter_recall_ablation import FilterAblationRow, FilterRecallAblationReport
from trader.domain.research.preregistered_rule_candidate import (
    PreregisteredRuleCandidateFamily,
    RuleCandidateMetrics,
    preregister_rule_candidates,
)
from trader.domain.research.preregistered_rule_candidate import (
    evaluate_rule_candidate_family as evaluate_family,
)


@dataclass(frozen=True)
class RuleCandidateEvaluation:
    family: PreregisteredRuleCandidateFamily
    metrics: tuple[RuleCandidateMetrics, ...]


def build_preregistered_rule_candidate_family(report: FilterRecallAblationReport) -> PreregisteredRuleCandidateFamily:
    return preregister_rule_candidates(report)


def evaluate_rule_candidate_family(
    family: PreregisteredRuleCandidateFamily, rows: tuple[FilterAblationRow, ...]
) -> RuleCandidateEvaluation:
    report = evaluate_family(family, rows)
    return RuleCandidateEvaluation(family, report.metrics)


__all__ = [
    "RuleCandidateEvaluation",
    "build_preregistered_rule_candidate_family",
    "evaluate_rule_candidate_family",
]
