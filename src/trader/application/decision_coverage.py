"""Shared typed coverage for decision queries and complete event replacements."""

from __future__ import annotations

from dataclasses import dataclass, fields

from trader.domain.recommendation.decision_identity import ScoredDecision
from trader.domain.recommendation.models import RecommendationAction


@dataclass(frozen=True)
class DecisionCoverage:
    candidate_count: int
    evaluated_count: int
    rejected_count: int
    selected_count: int
    executable_count: int
    observation_count: int

    def __post_init__(self) -> None:
        if any(getattr(self, item.name) < 0 for item in fields(self)):
            raise ValueError("decision coverage counts cannot be negative")
        if self.evaluated_count > self.candidate_count or self.rejected_count > self.candidate_count:
            raise ValueError("decision coverage cannot exceed candidate count")
        if self.selected_count > self.candidate_count:
            raise ValueError("selected decision coverage cannot exceed candidate count")
        if self.executable_count + self.observation_count != self.selected_count:
            raise ValueError("decision action coverage must partition selected count")


def scored_decision_coverage(decision: ScoredDecision) -> DecisionCoverage:
    rejected = (
        decision.rejected_count
        if decision.rejected_count is not None
        else sum(count for _reason, count in decision.filter_aggregates)
    )
    population = decision.population_count if decision.population_count is not None else len(decision.items) + rejected
    selected = tuple(item for item in decision.items if item.selected)
    return DecisionCoverage(
        candidate_count=population,
        evaluated_count=len(decision.items),
        rejected_count=rejected,
        selected_count=len(selected),
        executable_count=sum(item.action is RecommendationAction.EXECUTABLE for item in selected),
        observation_count=sum(item.action is RecommendationAction.OBSERVE for item in selected),
    )


__all__ = ["DecisionCoverage", "scored_decision_coverage"]
