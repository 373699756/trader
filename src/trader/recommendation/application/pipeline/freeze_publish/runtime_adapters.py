"""DeepSeek upgrade and freeze adapters for the unified scheduler."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from typing import Literal

from trader.recommendation.application.pipeline.data_source.source_router import MarketDataAdapter
from trader.recommendation.application.pipeline.freeze_publish.freeze_coordinator import ScoredFreezeCoordinator
from trader.recommendation.application.pipeline.policy import RecommendationPolicy
from trader.recommendation.application.pipeline.risk_review.deepseek_evidence_gate import normalize_scored_review_times
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
from trader.recommendation.domain.evidence.review import DeepSeekReview, ReviewOutcome
from trader.recommendation.domain.publication.decision_identity import DecisionIdentity, ScoredDecision
from trader.recommendation.domain.publication.models import Strategy


class DeepSeekAdapter(DeepSeekUpgradePort):
    def __init__(
        self,
        reviewer: TomorrowDeepSeekReviewPort,
        policy: RecommendationPolicy,
        data: MarketDataAdapter,
        fusion: ScoreFusionPort | None = None,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime],
    ) -> None:
        self._reviewer = reviewer
        self._policy = policy
        self._data = data
        self._fusion = fusion if fusion is not None else ScoreFusionService(monotonic=monotonic)
        self._monotonic = monotonic
        self._now = now

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
        started = self._monotonic()
        failure_reason = "deepseek_review_unavailable"
        try:
            reviews = self._reviewer.review(
                request.strategy,
                tuple(candidate.features for candidate in candidates),
                phase=request.phase,
                deadline=deadline,
                contexts={candidate.code: candidate.context for candidate in candidates},
            )
            failure_reason = "deepseek_manifest_validation_failed"
            expected = {
                candidate.code: self._reviewer.evidence_manifest_hash(candidate.features) for candidate in candidates
            }
            manifests_match = self._fusion.manifests_match(projection, reviews, expected)
            invalid_reason = (
                "deepseek_manifest_mismatch"
                if not manifests_match
                else "deepseek_review_time_invalid"
                if normalize_scored_review_times(reviews, deadline) is None
                else None
            )
        except (DeepSeekReviewUnavailableError, OSError, RuntimeError, TypeError, ValueError) as exc:
            failed_at = self._now()
            failed = {
                candidate.code: DeepSeekReview(
                    candidate.code, ReviewOutcome.REJECTED, {}, (), failed_at, error=failure_reason
                )
                for candidate in candidates
            }
            observed = self._fusion.fuse(
                projection,
                self._policy,
                failed,
                review_deadline=deadline,
                review_latency_ms=max(0, int((self._monotonic() - started) * 1000)),
            )
            if observed is not None:
                self._data.register_review(projection, observed)
            raise ReviewUnavailableError(type(exc).__name__) from exc
        if invalid_reason is not None:
            failed_at = self._now()
            reviews = {
                candidate.code: DeepSeekReview(
                    candidate.code, ReviewOutcome.REJECTED, {}, (), failed_at, error=invalid_reason
                )
                for candidate in candidates
            }
        hybrid = self._fusion.fuse(
            projection,
            self._policy,
            reviews,
            review_deadline=deadline,
            review_latency_ms=max(0, int((self._monotonic() - started) * 1000)),
        )
        if hybrid is not None:
            return self._data.register_review(projection, hybrid)
        return None


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
