"""Current market-input and freeze adapters used by the production scheduler."""

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
from trader.recommendation.application.pipeline.final_selection.freeze_projection import (
    ScoredLocalProjection,
)
from trader.recommendation.application.pipeline.freeze_publish.draft_index import UnifiedFreezeDraftIndex
from trader.recommendation.application.pipeline.local_score.base_scoring import (
    LocalScoringPort,
)
from trader.recommendation.application.pipeline.local_score.local_freeze_builder import (
    FreezeBuildDependencies,
    LocalFreezeBuilder,
)
from trader.recommendation.application.pipeline.policy import RecommendationPolicy
from trader.recommendation.application.ports.loaded_profile import ModelScoringPort
from trader.recommendation.application.ports.read_only_queries import ResearchAuditIdentity
from trader.recommendation.domain.publication.freeze_identity import (
    ScoredFreeze,
)


@dataclass(frozen=True)
class CycleFixtureDependencies:
    long_runtime: LongRuntime
    policy: RecommendationPolicy
    draft_index: UnifiedFreezeDraftIndex
    now: Callable[[], datetime]
    monotonic: Callable[[], float] = field(default=time.monotonic, kw_only=True)
    model_scoring: ModelScoringPort | None = None
    candidate_filtering: CandidateFilteringPort | None = None
    local_scoring: LocalScoringPort | None = None
    research_audit_builder: Callable[[ScoredLocalProjection, ScoredFreeze], ResearchAuditIdentity | None] = (
        lambda _projection, _freeze: None
    )


@dataclass(frozen=True)
class CycleFixture:
    data: MarketInputCoordinator
    freezes: LocalFreezeBuilder


def build_cycle_fixture(
    market: MarketReader, *, config_version: str, freeze_build: CycleFixtureDependencies
) -> CycleFixture:
    inputs = MarketInputCoordinator(
        market,
        dependencies=InputRefreshDependencies(
            freeze_build.long_runtime,
            freeze_build.policy,
            freeze_build.now,
            freeze_build.model_scoring,
            freeze_build.candidate_filtering,
            monotonic=freeze_build.monotonic,
        ),
    )
    freezes = LocalFreezeBuilder(
        inputs,
        config_version=config_version,
        dependencies=FreezeBuildDependencies(
            freeze_build.policy,
            freeze_build.draft_index,
            freeze_build.now,
            freeze_build.model_scoring,
            freeze_build.local_scoring,
            freeze_build.research_audit_builder,
            monotonic=freeze_build.monotonic,
            next_sequence=count(1, 2).__next__,
        ),
    )
    return CycleFixture(inputs, freezes)
