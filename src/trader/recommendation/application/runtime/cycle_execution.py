from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from trader.infra.sqlite_observation import IndependentSqliteWork

from trader.recommendation.application.pipeline.freeze_publish.current_publisher import (
    PreparedCurrentPair,
    PublishCurrentSnapshot,
)
from trader.recommendation.application.pipeline.freeze_publish.freeze_events import (
    FreezeCommitted,
)
from trader.recommendation.application.ports.loaded_profile import ModelScoringDeadlineError
from trader.recommendation.application.ports.runtime import (
    CycleRequest,
    DataRefreshUnavailableError,
    FreezeUnavailableError,
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
from trader.recommendation.domain.publication.freeze_identity import (
    FreezeIdentity,
    FreezeOverlay,
    ScoredFreeze,
)
from trader.recommendation.domain.publication.models import ScoredHeadGap, Strategy


_LOCAL_HEAD_REASONS = frozenset({
    "model_input_pending", "model_scoring_unavailable", "model_input_unavailable",
    "current_native_input_is_unavailable", "candidate_observations_are_unavailable",
    "scoring_input_identity_is_unavailable", "discovery_population_qualifications_are_unavailable",
    "future_input_time", "freeze_quote_unavailable", "candidate_data_pending",
})


@dataclass(frozen=True)
class HybridUpgradeRequest:
    local: ScoredFreeze
    cycle: CycleRequest


@dataclass(frozen=True, slots=True)
class _PreparedHeadCycle:
    request: CycleRequest
    freeze: ScoredFreeze
    overlay: FreezeOverlay


@dataclass(frozen=True)
class CycleExecutionHooks:
    local_lane: Callable[[Strategy], LatestWinsWorker[CycleRequest]]
    hybrid_lane: Callable[[Strategy], LatestWinsWorker[HybridUpgradeRequest]]
    failure: Callable[[str, str, Strategy | None], None]
    published: Callable[[bool, FreezeCommitted | None, FreezeIdentity], None]
    batch_published: Callable[[FreezeCommitted, FreezeIdentity], None]
    rejected: Callable[[str, Strategy], None]


class RecommendationCycleExecutor:
    """Execute immutable local/hybrid cycles without owning scheduler startup or shutdown."""

    def __init__(
        self,
        dependencies: RuntimeDependencies,
        *,
        close_control: CloseControlCoordinator,
        research: ResearchRuntimePort,
        hooks: CycleExecutionHooks,
    ) -> None:
        self._dependencies = dependencies
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
        finally:
            self._dependencies.data.release_scoring_input(request)

    def execute_round_pair(
        self,
        requests: tuple[CycleRequest, CycleRequest],
        *,
        round_sequence: int,
    ) -> None:
        """Stage this profile and continue only after the complete batch commits."""

        if (requests[0].strategy, requests[1].strategy) != (Strategy.TOMORROW, Strategy.D25):
            raise ValueError("paired cycle requires tomorrow and d25")
        scored = tuple(request for request in requests if request.strategy in self._dependencies.scored_strategies)
        try:
            with SessionGuard(requests[0].session), SessionGuard(requests[1].session):
                pass
            started_at = time.perf_counter()
            try:
                input_failures = self._dependencies.data.refresh_round_heads(scored)
            except DataRefreshUnavailableError as exc:
                self._hooks.failure("refresh", FailureCode(exc, "refresh_unavailable"), None)
                return
            self._record_latency("scoring_data_prepare", started_at)
            failures = {failure.strategy: failure.reason for failure in input_failures}
            ready: list[_PreparedHeadCycle] = []
            gaps: list[ScoredHeadGap] = []
            started_at = time.perf_counter()
            for request in scored:
                if self._dependencies.rounds.cancelled(round_sequence):
                    return
                input_failure = failures.get(request.strategy)
                outcome = (
                    self._head_gap(request, "refresh", input_failure)
                    if input_failure is not None else self._prepare_round_head(request)
                )
                if isinstance(outcome, ScoredHeadGap):
                    gaps.append(outcome)
                else:
                    ready.append(outcome)
            self._record_latency("local_scoring", started_at)
            if self._dependencies.rounds.cancelled(round_sequence):
                return
            freezes = tuple(head.freeze for head in ready)
            expected = tuple(
                current.version if current is not None else None
                for current in (self._dependencies.index.snapshot(item.strategy).current for item in freezes)
            )
            prepared = PreparedCurrentPair(
                self._dependencies.round_profile,
                self._dependencies.index,
                freezes,
                tuple(head.overlay for head in ready),
                expected_versions=expected,
                publication_io=self._dependencies.publication_io,
                sessions=tuple(request.session for request in requests),
                clock=self._dependencies.clock,
                publish_before=(
                    min(request.review_deadline for request in requests)
                    if requests[0].preview_context is None else None
                ),
                head_gaps=tuple(gaps),
            )
            publish_started = time.perf_counter()
            result = self._dependencies.rounds.stage(
                self._dependencies.round_profile,
                round_sequence,
                prepared,
            )
            self._record_latency("freeze_publish", publish_started)
            if not result.accepted:
                for request in requests:
                    self._hooks.rejected(result.reason, request.strategy)
                return
            for head, event in zip(ready, result.events, strict=True):
                request, local = head.request, head.freeze
                if request.session is not None:
                    request.session.remember(local.version)
                self._hooks.batch_published(event, local)
                with IndependentSqliteWork():
                    self._continue_after_local_publish(request, local, self._hooks.local_lane(request.strategy))
        except StaleSessionError:
            return
        finally:
            for request in scored:
                self._dependencies.data.release_scoring_input(request)

    def _prepare_round_head(self, request: CycleRequest) -> _PreparedHeadCycle | ScoredHeadGap:
        try:
            local = self._derive_local(request)
            if not isinstance(local, ScoredFreeze):
                return self._head_gap(request, "freeze", "scoring_output_unavailable")
            return _PreparedHeadCycle(request, local, self._dependencies.freezes.initial_overlay(local))
        except FreezeUnavailableError as exc:
            reason = FailureCode(exc, "freeze_unavailable")
            if isinstance(exc.__cause__, ModelScoringDeadlineError):
                reason = "model_scoring_deadline"
            elif reason not in _LOCAL_HEAD_REASONS:
                reason = "freeze_unavailable"
            return self._head_gap(request, "freeze", reason)

    def _head_gap(self, request: CycleRequest, stage: str, reason: str) -> ScoredHeadGap:
        with SessionGuard(request.session):
            self._hooks.failure(stage, reason, request.strategy)
        return ScoredHeadGap(request.strategy, request.trade_date, ShanghaiNow(self._dependencies.clock.now()), reason)

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
            if (
                snapshot.head_gap is None and isinstance(snapshot.current, ScoredFreeze)
                and snapshot.current.trade_date == request.trade_date
            ):
                if request.session is not None and not request.session.allows_version(snapshot.current.version):
                    return False
                versions = dict(snapshot.current.input_versions)
                close_key = snapshot.current.prediction_close_key
                if close_key is not None and not versions.get(close_key):
                    return False
                self._close_control.freeze_close_fallback(request, snapshot.current, recovery_path="current")
                return True
        return False

    def _build_fresh_local(
        self,
        request: CycleRequest,
    ) -> FreezeIdentity | None:
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
        local: FreezeIdentity,
        request: CycleRequest,
    ) -> bool:
        started_at = time.perf_counter()
        with SessionGuard(request.session):
            published = self._publish(local, hybrid=False)
            if published and request.session is not None:
                request.session.remember(local.version)
        if not published:
            return False
        self._record_latency("freeze_publish", started_at)
        return True

    def _continue_after_local_publish(
        self,
        request: CycleRequest,
        local: FreezeIdentity,
        lane: LatestWinsWorker[CycleRequest],
    ) -> None:
        if lane.is_superseded(request) or not isinstance(local, ScoredFreeze):
            return
        defer_initial_review = False if local.preview_context is not None else self._observe_research(local, request)
        if request.phase == "close_fallback":
            self._close_control.freeze_close_fallback(request, local, recovery_path="close_rebuild")
            return
        review_now = ShanghaiNow(self._dependencies.clock.now())
        if request.allow_review and not defer_initial_review and review_now < request.review_deadline:
            self._hooks.hybrid_lane(request.strategy).offer(HybridUpgradeRequest(local, request))

    def _observe_research(self, local: ScoredFreeze, request: CycleRequest) -> bool:
        try:
            with SessionGuard(request.session):
                return self._research.observe(
                    self._dependencies.freezes.research_intent(local),
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
        return True

    def _build_local(self, request: CycleRequest) -> FreezeIdentity | None:
        try:
            return self._derive_local(request)
        except FreezeUnavailableError as exc:
            with SessionGuard(request.session):
                self._hooks.failure("freeze", FailureCode(exc, "freeze_unavailable"), request.strategy)
            return None
    def _derive_local(self, request: CycleRequest) -> FreezeIdentity | None:
        local = self._dependencies.freezes.build_local(request)
        if local is not None:
            ValidateCycleIdentity(request, local)
        return local

    def _publish(self, identity: FreezeIdentity, *, hybrid: bool) -> bool:
        expected = self._dependencies.index.snapshot(identity.strategy).current
        try:
            published = PublishCurrentSnapshot(
                self._dependencies.index,
                self._dependencies.freezes,
                identity,
                expected_version=expected.version if expected is not None else None,
                publication_io=self._dependencies.publication_io,
            )
        except FreezeUnavailableError as exc:
            self._hooks.failure("freeze", FailureCode(exc, "freeze_quote_unavailable"), identity.strategy)
            return False
        if not published.accepted:
            self._hooks.rejected(published.reason, identity.strategy)
            return False
        self._hooks.published(hybrid, published.event, identity)
        return True

    def _upgrade_hybrid(self, local: ScoredFreeze, request: CycleRequest) -> None:
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
        except FreezeUnavailableError:
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
