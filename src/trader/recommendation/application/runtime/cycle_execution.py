from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from trader.recommendation.application.pipeline.freeze_publish.current_publisher import publish_current_snapshot
from trader.recommendation.application.pipeline.freeze_publish.decision_events import (
    DecisionCommitted,
)
from trader.recommendation.application.pipeline.freeze_publish.overlay_publisher import DecisionOverlayRefresher
from trader.recommendation.application.ports.runtime import (
    CycleRequest,
    DataRefreshUnavailableError,
    DecisionUnavailableError,
    ResearchRuntimePort,
    ReviewUnavailableError,
)
from trader.recommendation.application.runtime.close_control import CloseControlCoordinator
from trader.recommendation.application.runtime.latest_wins import (
    LatestWinsWorker,
)
from trader.recommendation.application.runtime.runtime_dependencies import RuntimeDependencies
from trader.recommendation.application.runtime.schedule import (
    shanghai_now,
)
from trader.recommendation.application.runtime.schedule_requests import (
    failure_code,
    validate_cycle_identity,
)
from trader.recommendation.domain.publication.decision_identity import (
    DecisionIdentity,
    ScoredDecision,
)
from trader.recommendation.domain.publication.models import Strategy


@dataclass(frozen=True)
class HybridUpgradeRequest:
    local: ScoredDecision
    cycle: CycleRequest


@dataclass(frozen=True)
class CycleExecutionHooks:
    local_lane: Callable[[Strategy], LatestWinsWorker[CycleRequest]]
    hybrid_lane: Callable[[Strategy], LatestWinsWorker[HybridUpgradeRequest]]
    failure: Callable[[str, str, Strategy | None], None]
    overlay_success: Callable[[Strategy, bool], None]
    published: Callable[[bool, DecisionCommitted | None, DecisionIdentity], None]
    rejected: Callable[[str, Strategy], None]
    long_handoff: Callable[[date], None]


class RecommendationCycleExecutor:
    """Execute immutable local/hybrid cycles without owning scheduler lifecycle."""

    def __init__(
        self,
        dependencies: RuntimeDependencies,
        *,
        overlay_refresher: DecisionOverlayRefresher,
        close_control: CloseControlCoordinator,
        research: ResearchRuntimePort,
        hooks: CycleExecutionHooks,
    ) -> None:
        self._dependencies = dependencies
        self._overlay_refresher = overlay_refresher
        self._close_control = close_control
        self._research = research
        self._hooks = hooks

    def execute(self, request: CycleRequest) -> None:
        lane = self._hooks.local_lane(request.strategy)
        if self._complete_existing_close_fallback(request):
            return
        local = self._build_fresh_local(request)
        if local is None or not self._publish_fresh_local(local):
            return
        self._continue_after_local_publish(request, local, lane)

    def _complete_existing_close_fallback(self, request: CycleRequest) -> bool:
        if request.phase == "close_fallback" and request.strategy in {Strategy.TOMORROW, Strategy.D25}:
            snapshot = self._dependencies.index.snapshot(request.strategy)
            if snapshot.formal is not None and snapshot.formal.trade_date == request.trade_date:
                return True
            if isinstance(snapshot.current, ScoredDecision) and snapshot.current.trade_date == request.trade_date:
                self._close_control.freeze_close_fallback(request, snapshot.current, recovery_path="current")
                return True
        return False

    def _build_fresh_local(
        self,
        request: CycleRequest,
    ) -> DecisionIdentity | None:
        started_at = time.perf_counter()
        if not self._prepare_cycle_data(request):
            return None
        self._record_latency("scoring_data_prepare", started_at)
        started_at = time.perf_counter()
        local = self._build_local(request)
        self._record_latency("local_scoring", started_at)
        if local is None:
            return None
        return local

    def _publish_fresh_local(
        self,
        local: DecisionIdentity,
    ) -> bool:
        started_at = time.perf_counter()
        if not self._publish(local, hybrid=False):
            return False
        self._record_latency("decision_publish", started_at)
        return True

    def _continue_after_local_publish(
        self,
        request: CycleRequest,
        local: DecisionIdentity,
        lane: LatestWinsWorker[CycleRequest],
    ) -> None:
        if lane.is_superseded(request) or not isinstance(local, ScoredDecision):
            return
        defer_initial_review = self._observe_research(local, request)
        if request.phase == "close_fallback":
            self._close_control.freeze_close_fallback(request, local, recovery_path="close_rebuild")
            return
        review_now = shanghai_now(self._dependencies.clock.now())
        if request.allow_review and not defer_initial_review and review_now < request.review_deadline:
            self._hooks.hybrid_lane(request.strategy).offer(HybridUpgradeRequest(local, request))

    def _observe_research(self, local: ScoredDecision, request: CycleRequest) -> bool:
        try:
            return self._research.observe(
                self._dependencies.decisions.research_intent(local),
                request,
            )
        except (RuntimeError, TypeError, ValueError) as exc:
            self._hooks.failure("research", failure_code(exc, "research_intent_failed"), request.strategy)
            return False

    def upgrade(self, request: HybridUpgradeRequest) -> None:
        if self._hooks.hybrid_lane(request.cycle.strategy).is_superseded(request):
            return
        if shanghai_now(self._dependencies.clock.now()) >= request.cycle.review_deadline:
            return
        self._upgrade_hybrid(request.local, request.cycle)

    def _record_latency(self, stage: str, started_at: float) -> None:
        self._dependencies.latency.record_duration(stage, (time.perf_counter() - started_at) * 1000.0)

    def _refresh_data(self, request: CycleRequest) -> bool:
        try:
            self._dependencies.data.refresh(request)
        except DataRefreshUnavailableError as exc:
            self._hooks.failure("refresh", failure_code(exc, "refresh_unavailable"), request.strategy)
            return False
        return True

    def _prepare_cycle_data(self, request: CycleRequest) -> bool:
        if not self._refresh_data(request):
            return False
        for outcome in self._overlay_refresher.refresh(request):
            if outcome.status == "failed":
                self._hooks.failure("overlay", outcome.error_code, outcome.strategy)
            elif outcome.status != "skipped":
                self._hooks.overlay_success(outcome.strategy, outcome.status == "published")
        if request.phase == "midday_recovery" and request.strategy is Strategy.LONG:
            self._hooks.long_handoff(request.trade_date)
        return True

    def _build_local(self, request: CycleRequest) -> DecisionIdentity | None:
        try:
            local = self._dependencies.decisions.build_local(request)
            if local is not None:
                validate_cycle_identity(request, local)
        except DecisionUnavailableError as exc:
            self._hooks.failure("decision", failure_code(exc, "decision_unavailable"), request.strategy)
            return None
        return local

    def _publish(self, identity: DecisionIdentity, *, hybrid: bool) -> bool:
        expected = self._dependencies.index.snapshot(identity.strategy).current
        try:
            published = publish_current_snapshot(
                self._dependencies.index,
                self._dependencies.decisions,
                identity,
                expected_version=expected.version if expected is not None else None,
                publication_io=self._dependencies.publication_io,
            )
        except DecisionUnavailableError as exc:
            self._hooks.failure("decision", failure_code(exc, "decision_quote_unavailable"), identity.strategy)
            return False
        if not published.accepted:
            self._hooks.rejected(published.reason, identity.strategy)
            return False
        self._hooks.published(hybrid, published.event, identity)
        return True

    def _upgrade_hybrid(self, local: ScoredDecision, request: CycleRequest) -> None:
        try:
            hybrid = self._dependencies.reviews.build_hybrid(local, request)
        except ReviewUnavailableError as exc:
            self._hooks.failure("review", failure_code(exc, "review_unavailable"), request.strategy)
            return
        if hybrid is None:
            return
        try:
            validate_cycle_identity(request, hybrid)
        except DecisionUnavailableError:
            self._hooks.failure("review", "review_identity_mismatch", request.strategy)
            return
        upgrade = HybridUpgradeRequest(local, request)
        self._hooks.hybrid_lane(request.strategy).execute_if_current(
            upgrade,
            lambda: self._publish(hybrid, hybrid=True),
        )
