"""DeepSeek upgrade and freeze adapters for the unified scheduler."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from trader.recommendation.application.pipeline.data_source.source_router import MarketDataAdapter
from trader.recommendation.application.pipeline.freeze_publish.freeze_coordinator import ScoredFreezeCoordinator
from trader.recommendation.application.pipeline.policy import RecommendationPolicy
from trader.recommendation.application.pipeline.score_merge.score_fusion import ScoreFusionPort, ScoreFusionService
from trader.recommendation.application.ports.deepseek import DeepSeekReviewUnavailableError, TomorrowDeepSeekReviewPort
from trader.recommendation.application.ports.runtime import (
    CycleRequest,
    DeepSeekUpgradePort,
    FreezePort,
    FreezeUnavailableError,
    ReviewUnavailableError,
    SharedDeepSeekRuntimeContract,
)
from trader.recommendation.domain.publication.decision_identity import DecisionIdentity, ScoredDecision
from trader.recommendation.domain.publication.models import Strategy


class DeepSeekAdapter(DeepSeekUpgradePort):
    def __init__(
        self,
        reviewer: TomorrowDeepSeekReviewPort,
        policy: RecommendationPolicy,
        data: MarketDataAdapter,
        fusion: ScoreFusionPort | None = None,
    ) -> None:
        self._reviewer = reviewer
        self._policy = policy
        self._data = data
        self._fusion = fusion if fusion is not None else ScoreFusionService()

    @property
    def runtime_contract(self) -> SharedDeepSeekRuntimeContract:
        return SharedDeepSeekRuntimeContract(168, True, True)

    def build_hybrid(self, local: ScoredDecision, request: CycleRequest) -> ScoredDecision | None:
        projection = self._data.projection(local.version)
        if projection is None:
            return None
        candidates = projection.review_candidates
        if not candidates:
            return None
        deadline = request.review_deadline
        try:
            reviews = self._reviewer.review(
                request.strategy,
                tuple(candidate.features for candidate in candidates),
                phase=request.phase,
                deadline=deadline,
                contexts={candidate.code: candidate.context for candidate in candidates},
            )
        except (DeepSeekReviewUnavailableError, OSError, RuntimeError, TypeError, ValueError) as exc:
            raise ReviewUnavailableError(type(exc).__name__) from exc
        expected = {
            candidate.code: self._reviewer.evidence_manifest_hash(candidate.features) for candidate in candidates
        }
        if not self._fusion.manifests_match(projection, reviews, expected):
            return None
        hybrid = self._fusion.fuse(projection, self._policy, reviews, review_deadline=deadline)
        if hybrid is not None:
            hybrid = self._data.register_hybrid(projection, hybrid)
        return hybrid


class FreezeAdapter(FreezePort):
    def __init__(
        self,
        tomorrow: ScoredFreezeCoordinator,
        d25: ScoredFreezeCoordinator,
    ) -> None:
        self._freezers: dict[Strategy, ScoredFreezeCoordinator] = {
            Strategy.TOMORROW: tomorrow,
            Strategy.D25: d25,
        }

    def capture_checkpoint(self, strategy: Strategy, at: datetime) -> None:
        del at
        freezer = self._freezers.get(strategy)
        if freezer is None:
            raise FreezeUnavailableError("checkpoint is only available for tomorrow and d25")
        result = freezer.capture_checkpoint()
        if result.status != "checkpoint_saved":
            raise FreezeUnavailableError(result.status)

    def freeze(self, strategy: Strategy, at: datetime, current: DecisionIdentity | None) -> None:
        del at, current
        result = self._freezers[strategy].freeze_scheduled()
        if result.status in {"persistence_failed", "index_commit_conflict"}:
            raise RuntimeError(result.status)

    def freeze_close_fallback(
        self,
        strategy: Strategy,
        at: datetime,
        current: ScoredDecision,
        *,
        recovery_path: Literal["current", "close_rebuild"],
        official_close_version: str,
    ) -> None:
        del at
        freezer = self._freezers.get(strategy)
        if freezer is None:
            raise FreezeUnavailableError("close fallback is only available for tomorrow and d25")
        result = freezer.freeze_close_fallback(
            current,
            recovery_path=recovery_path,
            official_close_version=official_close_version,
        )
        if result.status not in {"frozen", "already_frozen"}:
            raise FreezeUnavailableError(result.status)


__all__ = ["DeepSeekAdapter", "FreezeAdapter"]
