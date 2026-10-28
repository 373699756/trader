"""Shared typed coverage for freeze queries and complete event replacements."""

from __future__ import annotations

from dataclasses import dataclass

from trader.recommendation.domain.publication.freeze_identity import ScoredFreeze
from trader.recommendation.domain.publication.models import RecommendationAction


@dataclass(frozen=True)
class FreezeCoverage:
    candidate_count: int
    evaluated_count: int
    rejected_count: int
    selected_count: int
    executable_count: int
    observation_count: int
    unavailable_count: int = 0

    def __post_init__(self) -> None:
        counts = (
            self.candidate_count,
            self.evaluated_count,
            self.rejected_count,
            self.selected_count,
            self.executable_count,
            self.observation_count,
            self.unavailable_count,
        )
        if any(value < 0 for value in counts):
            raise ValueError("freeze coverage counts cannot be negative")
        if self.evaluated_count > self.candidate_count or self.rejected_count > self.candidate_count:
            raise ValueError("freeze coverage cannot exceed candidate count")
        if self.selected_count > self.candidate_count:
            raise ValueError("selected freeze coverage cannot exceed candidate count")
        if self.executable_count + self.observation_count + self.unavailable_count != self.selected_count:
            raise ValueError("freeze action coverage must partition selected count")


def ScoredFreezeCoverage(freeze: ScoredFreeze) -> FreezeCoverage:
    rejected = (
        freeze.rejected_count
        if freeze.rejected_count is not None
        else sum(count for _reason, count in freeze.filter_aggregates)
    )
    population = freeze.population_count if freeze.population_count is not None else len(freeze.items) + rejected
    selected = tuple(item for item in freeze.items if item.selected)
    diagnostics = freeze.selection_diagnostics
    evaluated = (
        diagnostics.evaluated_count
        if diagnostics is not None and diagnostics.evaluated_count is not None
        else len(freeze.items)
    )
    return FreezeCoverage(
        candidate_count=population,
        evaluated_count=evaluated,
        rejected_count=rejected,
        selected_count=len(selected),
        executable_count=sum(item.action is RecommendationAction.EXECUTABLE for item in selected),
        observation_count=sum(item.action is RecommendationAction.OBSERVE for item in selected),
        unavailable_count=sum(item.action is RecommendationAction.UNAVAILABLE for item in selected),
    )


__all__ = ["FreezeCoverage", "ScoredFreezeCoverage"]
