from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from trader.recommendation.application.pipeline.freeze_publish.current_publisher import (
    PublishCurrentPair,
    PublishCurrentSnapshot,
)
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
    ShanghaiNow,
)
from trader.recommendation.application.runtime.schedule_requests import (
    FailureCode,
    ValidateCycleIdentity,
)
from trader.recommendation.application.runtime.trading_session import SessionGuard, StaleSessionError
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


class RecommendationCycleExecutor:
    """Execute immutable local/hybrid cycles without owning scheduler startup or shutdown."""

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
        try:
            with SessionGuard(request.session):
                pass
            self._execute(request)
        except StaleSessionError:
            return

    def execute_pair(self, requests: tuple[CycleRequest, CycleRequest]) -> None:
        """Publish one local Tomorrow/D25 wave only after both inputs are ready."""

        if (requests[0].strategy, requests[1].strategy) != (Strategy.TOMORROW, Strategy.D25):
            raise ValueError("paired cycle requires tomorrow and d25")
        try:
            with SessionGuard(requests[0].session), SessionGuard(requests[1].session):
                pass
            decisions = self._build_pair(requests)
            if decisions is None or not self._publish_local_pair(requests, decisions):
                return
            for request, local in zip(requests, decisions, strict=True):
                self._continue_after_local_publish(request, local, self._hooks.local_lane(request.strategy))
        except StaleSessionError:
            return

    def _build_pair(self, requests: tuple[CycleRequest, CycleRequest]) -> tuple[ScoredDecision, ScoredDecision] | None:
        started_at = time.perf_counter()
        shared_epoch = self._dependencies.data.scoring_epoch()
        if not all(self._prepare_cycle_data(request) for request in requests):
            return None
        if self._dependencies.data.scoring_epoch() != shared_epoch:
            self._hooks.failure("decision", "paired_input_epoch_mismatch", None)
            return None
        self._record_latency("scoring_data_prepare", started_at)
        started_at = time.perf_counter()
        tomorrow = self._build_local(requests[0])
        if not isinstance(tomorrow, ScoredDecision):
            return None
        d25 = self._build_local(requests[1])
        if not isinstance(d25, ScoredDecision):
            return None
        self._record_latency("local_scoring", started_at)
        return tomorrow, d25

    def _publish_local_pair(
        self,
        requests: tuple[CycleRequest, CycleRequest],
        decisions: tuple[ScoredDecision, ScoredDecision],
    ) -> bool:
        expected = tuple(
            current.version if current is not None else None
            for current in (self._dependencies.index.snapshot(item.strategy).current for item in decisions)
        )
        try:
            with SessionGuard(requests[0].session), SessionGuard(requests[1].session):
                result = PublishCurrentPair(
                    self._dependencies.index,
                    self._dependencies.decisions,
                    decisions,
                    expected_versions=(expected[0], expected[1]),
                    publication_io=self._dependencies.publication_io,
                )
                if result.accepted:
                    for request, local in zip(requests, decisions, strict=True):
                        if request.session is not None:
                            request.session.remember(local.version)
        except DecisionUnavailableError as exc:
            self._hooks.failure("decision", FailureCode(exc, "decision_quote_unavailable"), None)
            return False
        if not result.accepted:
            for request in requests:
                self._hooks.rejected(result.reason, request.strategy)
            return False
        for local, event in zip(decisions, result.events, strict=True):
            self._hooks.published(False, event, local)
        return True

    def _execute(self, request: CycleRequest) -> None:
        lane = self._hooks.local_lane(request.strategy)
        if self._complete_existing_close_fallback(request):
            return
        local = self._build_fresh_local(request)
        if local is None or not self._publish_fresh_local(local, request):
            return
        self._continue_after_local_publish(request, local, lane)

    def _complete_existing_close_fallback(self, request: CycleRequest) -> bool:
        if request.phase == "close_fallback" and request.strategy in {Strategy.TOMORROW, Strategy.D25}:
            snapshot = self._dependencies.index.snapshot(request.strategy)
            if snapshot.formal is not None and snapshot.formal.trade_date == request.trade_date:
                return True
            if isinstance(snapshot.current, ScoredDecision) and snapshot.current.trade_date == request.trade_date:
                if request.session is not None and not request.session.allows_version(snapshot.current.version):
                    return False
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
        request: CycleRequest,
    ) -> bool:
        started_at = time.perf_counter()
        with SessionGuard(request.session):
            published = self._publish(local, hybrid=False)
            if published and request.session is not None:
                request.session.remember(local.version)
        if not published:
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
        review_now = ShanghaiNow(self._dependencies.clock.now())
        if request.allow_review and not defer_initial_review and review_now < request.review_deadline:
            self._hooks.hybrid_lane(request.strategy).offer(HybridUpgradeRequest(local, request))

    def _observe_research(self, local: ScoredDecision, request: CycleRequest) -> bool:
        try:
            with SessionGuard(request.session):
                return self._research.observe(
                    self._dependencies.decisions.research_intent(local),
                    request,
                )
        except StaleSessionError:
            raise
        except (RuntimeError, TypeError, ValueError) as exc:
            self._hooks.failure("research", FailureCode(exc, "research_intent_failed"), request.strategy)
            return False

    def upgrade(self, request: HybridUpgradeRequest) -> None:
        if self._hooks.hybrid_lane(request.cycle.strategy).is_superseded(request):
            return
        if ShanghaiNow(self._dependencies.clock.now()) >= request.cycle.review_deadline:
            return
        try:
            with SessionGuard(request.cycle.session):
                pass
            self._upgrade_hybrid(request.local, request.cycle)
        except StaleSessionError:
            return

    def _record_latency(self, stage: str, started_at: float) -> None:
        self._dependencies.latency.record_duration(stage, (time.perf_counter() - started_at) * 1000.0)

    def _refresh_data(self, request: CycleRequest) -> bool:
        try:
            self._dependencies.data.refresh(request)
        except DataRefreshUnavailableError as exc:
            with SessionGuard(request.session):
                self._hooks.failure("refresh", FailureCode(exc, "refresh_unavailable"), request.strategy)
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
        return True

    def _build_local(self, request: CycleRequest) -> DecisionIdentity | None:
        try:
            local = self._dependencies.decisions.build_local(request)
            if local is not None:
                ValidateCycleIdentity(request, local)
        except DecisionUnavailableError as exc:
            with SessionGuard(request.session):
                self._hooks.failure("decision", FailureCode(exc, "decision_unavailable"), request.strategy)
            return None
        return local

    def _publish(self, identity: DecisionIdentity, *, hybrid: bool) -> bool:
        expected = self._dependencies.index.snapshot(identity.strategy).current
        try:
            published = PublishCurrentSnapshot(
                self._dependencies.index,
                self._dependencies.decisions,
                identity,
                expected_version=expected.version if expected is not None else None,
                publication_io=self._dependencies.publication_io,
            )
        except DecisionUnavailableError as exc:
            self._hooks.failure("decision", FailureCode(exc, "decision_quote_unavailable"), identity.strategy)
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
            with SessionGuard(request.session):
                self._hooks.failure("review", FailureCode(exc, "review_unavailable"), request.strategy)
            return
        if hybrid is None:
            return
        try:
            ValidateCycleIdentity(request, hybrid)
        except DecisionUnavailableError:
            self._hooks.failure("review", "review_identity_mismatch", request.strategy)
            return
        upgrade = HybridUpgradeRequest(local, request)
        with SessionGuard(request.session):
            published = self._hooks.hybrid_lane(request.strategy).execute_if_current(
                upgrade,
                lambda: self._publish(hybrid, hybrid=True),
            )
            if published and request.session is not None:
                request.session.remember(hybrid.version)
