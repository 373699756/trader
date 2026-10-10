"""Current market-input and decision adapters used by the production scheduler."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from itertools import count

from trader.recommendation.application.long_runtime import LongRuntime
from trader.recommendation.application.pipeline.candidate_pool.candidate_pool_service import (
    CandidateFilteringPort,
)
from trader.recommendation.application.pipeline.data_source.source_router import (
    InputRefreshDependencies,
    MarketInputCoordinator,
    MarketReader,
)
from trader.recommendation.application.pipeline.final_selection.decision_projection import (
    ScoredLocalProjection,
)
from trader.recommendation.application.pipeline.freeze_publish.draft_index import UnifiedDecisionDraftIndex
from trader.recommendation.application.pipeline.local_score.base_scoring import (
    LocalScoringPort,
)
from trader.recommendation.application.pipeline.local_score.local_decision_builder import (
    DecisionBuildDependencies,
    LocalDecisionBuilder,
)
from trader.recommendation.application.pipeline.policy import RecommendationPolicy
from trader.recommendation.application.ports.loaded_profile import ModelScoringPort
from trader.recommendation.application.ports.read_only_queries import ResearchAuditIdentity
from trader.recommendation.domain.publication.decision_identity import (
    ScoredDecision,
)


@dataclass(frozen=True)
class CycleFixtureDependencies:
    long_runtime: LongRuntime
    policy: RecommendationPolicy
    draft_index: UnifiedDecisionDraftIndex
    now: Callable[[], datetime]
    monotonic: Callable[[], float] = field(default=time.monotonic, kw_only=True)
    model_scoring: ModelScoringPort | None = None
    candidate_filtering: CandidateFilteringPort | None = None
    local_scoring: LocalScoringPort | None = None
    research_audit_builder: Callable[[ScoredLocalProjection, ScoredDecision], ResearchAuditIdentity | None] = (
        lambda _projection, _decision: None
    )


@dataclass(frozen=True)
class CycleFixture:
    data: MarketInputCoordinator
    decisions: LocalDecisionBuilder


def build_cycle_fixture(
    market: MarketReader, *, config_version: str, candidate_pool_size: int, decision_build: CycleFixtureDependencies
) -> CycleFixture:
    inputs = MarketInputCoordinator(
        market,
        candidate_pool_size=candidate_pool_size,
        dependencies=InputRefreshDependencies(
            decision_build.long_runtime,
            decision_build.policy,
            decision_build.now,
            decision_build.model_scoring,
            decision_build.candidate_filtering,
            monotonic=decision_build.monotonic,
        ),
    )
    decisions = LocalDecisionBuilder(
        inputs,
        config_version=config_version,
        candidate_pool_size=candidate_pool_size,
        dependencies=DecisionBuildDependencies(
            decision_build.policy,
            decision_build.draft_index,
            decision_build.now,
            decision_build.model_scoring,
            decision_build.local_scoring,
            decision_build.research_audit_builder,
            monotonic=decision_build.monotonic,
            next_sequence=count(1, 2).__next__,
        ),
    )
    return CycleFixture(inputs, decisions)
