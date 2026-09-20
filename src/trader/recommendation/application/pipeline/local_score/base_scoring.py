"""Deterministic base and local scoring boundary for recommendation runs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from trader.recommendation.application.pipeline.downside_action.downside_protection import RiskControlPort
from trader.recommendation.application.pipeline.final_selection.decision_projection import (
    ScoredLocalProjection,
    ScoredProjectionInputs,
    build_scored_local,
)
from trader.recommendation.application.pipeline.final_selection.grouped_ranking import RankingSelectionPort
from trader.recommendation.application.pipeline.policy import RecommendationPolicy
from trader.recommendation.application.ports.loaded_profile import ModelScoringContext, ModelScoringPort
from trader.recommendation.application.ports.scoring import ScoredNativeInput
from trader.recommendation.domain.selection.scored_selection import ScoredCandidateStageCounts


@dataclass(frozen=True)
class LocalScoringContext:
    """Per-run timing and candidate diagnostics, without service dependencies."""

    model_context: ModelScoringContext | None = None
    candidate_stage_counts: ScoredCandidateStageCounts | None = None
    preselection_transient_invalid: bool = False


class LocalScoringPort(Protocol):
    """Build a local decision from prepared native input only."""

    def score(
        self,
        native_input: ScoredNativeInput,
        policy: RecommendationPolicy,
        *,
        sequence: int,
        context: LocalScoringContext | None = None,
    ) -> ScoredLocalProjection: ...


@dataclass(frozen=True)
class LocalScoringService(LocalScoringPort):
    """Stateless local scorer that delegates to the deterministic projection."""

    model_scoring: ModelScoringPort | None = None
    ranking_selection: RankingSelectionPort | None = None
    risk_control: RiskControlPort | None = None

    def score(
        self,
        native_input: ScoredNativeInput,
        policy: RecommendationPolicy,
        *,
        sequence: int,
        context: LocalScoringContext | None = None,
    ) -> ScoredLocalProjection:
        context = context if context is not None else LocalScoringContext()
        return build_scored_local(
            native_input,
            policy,
            sequence=sequence,
            runtime=ScoredProjectionInputs(
                model_scoring=self.model_scoring,
                scoring_context=context.model_context,
                candidate_stage_counts=context.candidate_stage_counts,
                preselection_transient_invalid=context.preselection_transient_invalid,
                ranking_selection=self.ranking_selection,
                risk_control=self.risk_control,
            ),
        )


__all__ = ["LocalScoringContext", "LocalScoringPort", "LocalScoringService"]
