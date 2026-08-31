"""DeepSeek upgrade and freeze adapters for the unified V2 scheduler."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from trader.application.ports.reviews import DeepSeekReviewUnavailableError, TomorrowDeepSeekReviewPort
from trader.application.ports.v2_runtime import (
    SharedDeepSeekRuntimeContract,
    V2CycleRequest,
    V2DeepSeekUpgradePort,
    V2FreezePort,
    V2FreezeUnavailableError,
    V2ReviewUnavailableError,
)
from trader.application.recommendation.policy import RecommendationPolicy
from trader.application.recommendation.scored_v2_freezing import ScoredV2FreezeCoordinator
from trader.application.recommendation.scored_v2_projection import build_scored_v2_hybrid, validate_review_manifests
from trader.application.recommendation.today_v2_freezing import TodayV2FreezeCoordinator
from trader.application.v2_input_runtime import V2MarketDataAdapter
from trader.domain.recommendation.decision_identity import DecisionIdentity, ScoredDecision
from trader.domain.recommendation.models import Strategy


class V2DeepSeekAdapter(V2DeepSeekUpgradePort):
    def __init__(
        self,
        reviewer: TomorrowDeepSeekReviewPort,
        policy: RecommendationPolicy,
        data: V2MarketDataAdapter,
    ) -> None:
        self._reviewer = reviewer
        self._policy = policy
        self._data = data

    @property
    def runtime_contract(self) -> SharedDeepSeekRuntimeContract:
        return SharedDeepSeekRuntimeContract(168, True, True)

    def build_hybrid(self, local: ScoredDecision, request: V2CycleRequest) -> ScoredDecision | None:
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
            raise V2ReviewUnavailableError(type(exc).__name__) from exc
        expected = {
            candidate.code: self._reviewer.evidence_manifest_hash(candidate.features) for candidate in candidates
        }
        if not validate_review_manifests(projection, reviews, expected):
            return None
        hybrid = build_scored_v2_hybrid(projection, self._policy, reviews, review_deadline=deadline)
        if hybrid is not None:
            self._data.register_hybrid(projection, hybrid)
        return hybrid


class V2FreezeAdapter(V2FreezePort):
    def __init__(
        self,
        today: TodayV2FreezeCoordinator,
        tomorrow: ScoredV2FreezeCoordinator,
        d25: ScoredV2FreezeCoordinator,
    ) -> None:
        self._freezers: dict[Strategy, TodayV2FreezeCoordinator | ScoredV2FreezeCoordinator] = {
            Strategy.TODAY: today,
            Strategy.TOMORROW: tomorrow,
            Strategy.D25: d25,
        }

    def capture_checkpoint(self, strategy: Strategy, at: datetime) -> None:
        del at
        freezer = self._freezers.get(strategy)
        if not isinstance(freezer, ScoredV2FreezeCoordinator):
            raise V2FreezeUnavailableError("checkpoint is only available for tomorrow and d25")
        result = freezer.capture_checkpoint()
        if result.status != "checkpoint_saved":
            raise V2FreezeUnavailableError(result.status)

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
        if not isinstance(freezer, ScoredV2FreezeCoordinator):
            raise V2FreezeUnavailableError("close fallback is only available for tomorrow and d25")
        result = freezer.freeze_close_fallback(
            current,
            recovery_path=recovery_path,
            official_close_version=official_close_version,
        )
        if result.status not in {"frozen", "already_frozen"}:
            raise V2FreezeUnavailableError(result.status)


__all__ = ["V2DeepSeekAdapter", "V2FreezeAdapter"]
