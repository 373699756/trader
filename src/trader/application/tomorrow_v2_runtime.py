"""Production Tomorrow V2 native-input lane and formal freeze control."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, time
from typing import Literal

from trader.application.decision_core import UnifiedDecisionIndex
from trader.application.decision_events import (
    V2DecisionCommitted,
    build_v2_decision_committed,
)
from trader.application.decision_observers import DecisionObserverRuntime
from trader.application.policy import RecommendationPolicy
from trader.application.ports.clock import Clock
from trader.application.ports.reviews import DeepSeekReviewUnavailableError, TomorrowDeepSeekReviewPort
from trader.application.ports.tomorrow import ScoredNativeInput
from trader.application.research_audit import V2DecisionObservation, try_build_v2_committed_research_audit
from trader.application.shutdown import ShutdownDeadline, ShutdownStep
from trader.application.tomorrow_v2_freezing import TomorrowV2FreezeCoordinator, V2FreezeOperationResult
from trader.application.tomorrow_v2_projection import (
    TomorrowV2LocalProjection,
    build_tomorrow_v2_hybrid,
    build_tomorrow_v2_local,
    validate_review_manifests,
)
from trader.application.v2_lifecycle import LatestWinsStatus, LatestWinsWorker
from trader.domain.recommendation.decision_identity import ScoredDecision
from trader.domain.recommendation.models import Strategy


@dataclass(frozen=True)
class TomorrowV2RuntimeStatus:
    worker: LatestWinsStatus
    local_publish_count: int
    hybrid_publish_count: int
    publish_rejection_count: int
    review_failure_count: int
    review_late_count: int
    observer_rejection_count: int
    input_rejection_count: int
    checkpoint_status: str
    freeze_status: str
    last_error_code: str


@dataclass(frozen=True)
class TomorrowV2RuntimeDependencies:
    reviewer: TomorrowDeepSeekReviewPort
    index: UnifiedDecisionIndex
    observer: DecisionObserverRuntime
    freezer: TomorrowV2FreezeCoordinator
    clock: Clock


class TomorrowV2Runtime:
    """One latest-wins producer driven exclusively by native point-in-time inputs."""

    def __init__(
        self,
        policy: RecommendationPolicy,
        dependencies: TomorrowV2RuntimeDependencies,
        *,
        strategy: Strategy = Strategy.TOMORROW,
    ) -> None:
        self._policy = policy
        self._strategy = strategy
        self._reviewer = dependencies.reviewer
        self._index = dependencies.index
        self._observer = dependencies.observer
        self._freezer = dependencies.freezer
        self._clock = dependencies.clock
        self._lock = threading.RLock()
        self._sequence = 1
        self._local_publish_count = 0
        self._hybrid_publish_count = 0
        self._publish_rejection_count = 0
        self._review_failure_count = 0
        self._review_late_count = 0
        self._observer_rejection_count = 0
        self._input_rejection_count = 0
        self._checkpoint_status = "not_attempted"
        self._freeze_status = "not_attempted"
        self._last_error_code = ""
        self._worker = LatestWinsWorker(
            self._worker_name(),
            self._process,
            order_key=lambda item: int(item.evaluated_at.timestamp() * 1_000_000),
        )

    def start(self) -> bool:
        if not self._observer.start():
            return False
        try:
            started = self._worker.start()
        except BaseException:
            self._observer.close()
            self._observer.stop(deadline=ShutdownDeadline.start(5.0))
            raise
        formal = self._index.snapshot(self._strategy).formal
        if formal is not None:
            self._observer.offer(V2DecisionObservation(build_v2_decision_committed(formal.decision), None))
        return started

    def initialize(self) -> None:
        restored = self._freezer.restore(self._clock.now().date())
        if restored.status == "persistence_failed":
            with self._lock:
                self._freeze_status = restored.status
                self._last_error_code = "freeze:persistence_failed"
        if restored.record is not None and self._observer.status().accepting:
            self._offer_formal_event(restored.record.decision)

    def offer_native(self, native_input: ScoredNativeInput) -> bool:
        if native_input.strategy is not self._strategy:
            self._reject_input("wrong_strategy")
            return False
        return self._worker.offer(native_input).value in {"accepted", "replaced", "coalesced"}

    def wait_idle(self, timeout_seconds: float) -> bool:
        return self._worker.wait_idle(timeout_seconds)

    def stop(
        self,
        *,
        wait: bool,
        deadline: ShutdownDeadline | None = None,
    ) -> ShutdownStep:
        shared = deadline or ShutdownDeadline.start(30.0)
        if not wait:
            self._worker.close()
            self._observer.close()
            return ShutdownStep(name=f"{self._worker_name()}-close", completed=True, timed_out=False)
        worker = self._worker.stop(deadline=shared)
        observer = self._observer.stop(deadline=shared)
        completed = worker.completed and observer.completed
        return ShutdownStep(
            name=self._worker_name(),
            completed=completed,
            timed_out=not completed and shared.expired,
            cancelled_count=worker.cancelled_count + observer.cancelled_count,
            detail="" if completed else f"{self._strategy.value} V2 runtime remains active",
        )

    def on_clock(self, at: datetime) -> V2FreezeOperationResult | None:
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("Tomorrow V2 control time must be timezone-aware")
        local = at.astimezone(self._clock.now().tzinfo)
        checkpoint_start = datetime.combine(local.date(), time(14, 49, 20), tzinfo=local.tzinfo)
        boundary = datetime.combine(local.date(), time(14, 50), tzinfo=local.tzinfo)
        close = datetime.combine(local.date(), time(15, 0), tzinfo=local.tzinfo)
        result: V2FreezeOperationResult | None = None
        if checkpoint_start <= local < boundary:
            result = self._freezer.capture_checkpoint()
            with self._lock:
                self._checkpoint_status = result.status
        elif boundary <= local < close:
            result = self._freezer.freeze_scheduled()
            with self._lock:
                self._freeze_status = result.status
            if result.status == "frozen" and result.record is not None:
                self._offer_formal_event(result.record.decision)
        return result

    def recover_close_fallback(
        self,
        *,
        official_close_version: str,
        recovery_path: Literal["current", "close_rebuild"] = "current",
    ) -> V2FreezeOperationResult:
        current = self._index.snapshot(self._strategy).current
        if not isinstance(current, ScoredDecision):
            return V2FreezeOperationResult("no_eligible_decision")
        if recovery_path not in {"current", "close_rebuild"}:
            raise ValueError(f"{self._strategy.value} V2 close recovery path is invalid")
        result = self._freezer.freeze_close_fallback(
            current,
            recovery_path=recovery_path,
            official_close_version=official_close_version,
        )
        with self._lock:
            self._freeze_status = result.status
        if result.status == "frozen" and result.record is not None:
            self._offer_formal_event(result.record.decision)
        return result

    def status(self) -> TomorrowV2RuntimeStatus:
        with self._lock:
            return TomorrowV2RuntimeStatus(
                self._worker.status(),
                self._local_publish_count,
                self._hybrid_publish_count,
                self._publish_rejection_count,
                self._review_failure_count,
                self._review_late_count,
                self._observer_rejection_count,
                self._input_rejection_count,
                self._checkpoint_status,
                self._freeze_status,
                self._last_error_code,
            )

    def _process(self, native_input: ScoredNativeInput) -> None:
        if native_input.strategy is not self._strategy:
            self._reject_input("wrong_strategy")
            return
        if native_input.phase == "close_fallback":
            self._process_close_fallback(native_input)
        else:
            self._process_regular(native_input)

    def _process_regular(self, native_input: ScoredNativeInput) -> None:
        sequence = self._next_sequence()
        projection = build_tomorrow_v2_local(native_input, self._policy, sequence=sequence)
        if not projection.input_quality.publishable:
            self._reject_input(projection.input_quality.status)
            return
        local = projection.local
        expected = self._index.snapshot(self._strategy).current
        published = self._index.publish(
            local,
            expected_version=expected.version if expected is not None else None,
        )
        if not published.accepted:
            self._reject_publish(published.reason)
            return
        self._record_publish(published.event, hybrid=False, projection=projection, decision=local)
        self._try_hybrid_upgrade(native_input, projection, local)

    def _try_hybrid_upgrade(
        self,
        native_input: ScoredNativeInput,
        projection: TomorrowV2LocalProjection,
        local: ScoredDecision,
    ) -> None:
        deadline = datetime.combine(native_input.trade_date, time(14, 48), tzinfo=native_input.evaluated_at.tzinfo)
        if native_input.evaluated_at >= deadline or self._clock.now() >= deadline:
            with self._lock:
                self._review_late_count += 1
            return
        if not projection.review_candidates:
            return
        expected_manifests = {
            candidate.code: self._reviewer.evidence_manifest_hash(candidate.features)
            for candidate in projection.review_candidates
        }
        try:
            reviews = dict(
                self._reviewer.review(
                    self._strategy,
                    tuple(candidate.features for candidate in projection.review_candidates),
                    phase=native_input.phase,
                    deadline=deadline,
                    contexts={candidate.code: candidate.context for candidate in projection.review_candidates},
                )
            )
        except DeepSeekReviewUnavailableError:
            with self._lock:
                self._review_failure_count += 1
                self._last_error_code = "deepseek_transport_failed"
            return
        if not validate_review_manifests(projection, reviews, expected_manifests):
            with self._lock:
                self._review_failure_count += 1
                self._last_error_code = "deepseek_identity_rejected"
            return
        hybrid = build_tomorrow_v2_hybrid(projection, self._policy, reviews, review_deadline=deadline)
        if hybrid is None:
            with self._lock:
                self._review_late_count += int(any(review.completed_at >= deadline for review in reviews.values()))
            return
        upgraded = self._index.publish(hybrid, expected_version=local.version)
        if not upgraded.accepted:
            self._reject_publish(upgraded.reason)
            return
        self._record_publish(upgraded.event, hybrid=True, projection=projection, decision=hybrid)

    def _process_close_fallback(self, native_input: ScoredNativeInput) -> None:
        official_close_version = f"official-close:{native_input.input_version.removeprefix('native-input:')}"
        current = self._index.snapshot(self._strategy).current
        recovery_path: Literal["current", "close_rebuild"] = "current"
        if not isinstance(current, ScoredDecision) or current.trade_date != native_input.trade_date:
            projection = build_tomorrow_v2_local(
                native_input,
                self._policy,
                sequence=self._next_sequence(),
            )
            if not projection.input_quality.publishable:
                self._reject_input(projection.input_quality.status)
                return
            current = projection.local
            expected = self._index.snapshot(self._strategy).current
            published = self._index.publish(
                current,
                expected_version=expected.version if expected is not None else None,
            )
            if not published.accepted:
                self._reject_publish(published.reason)
                return
            self._record_publish(published.event, hybrid=False, projection=projection, decision=current)
            recovery_path = "close_rebuild"
        result = self._freezer.freeze_close_fallback(
            current,
            recovery_path=recovery_path,
            official_close_version=official_close_version,
        )
        with self._lock:
            self._freeze_status = result.status
        if result.status == "frozen" and result.record is not None:
            self._offer_formal_event(result.record.decision)

    def _next_sequence(self) -> int:
        with self._lock:
            sequence = self._sequence
            self._sequence += 2
            return sequence

    def _record_publish(
        self,
        event: V2DecisionCommitted | None,
        *,
        hybrid: bool,
        projection: TomorrowV2LocalProjection,
        decision: ScoredDecision,
    ) -> None:
        with self._lock:
            if hybrid:
                self._hybrid_publish_count += 1
            else:
                self._local_publish_count += 1
        observation = (
            V2DecisionObservation(event, try_build_v2_committed_research_audit(projection, decision))
            if event is not None
            else None
        )
        if observation is not None and not self._observer.offer(observation):
            with self._lock:
                self._observer_rejection_count += 1

    def _reject_publish(self, reason: str) -> None:
        with self._lock:
            self._publish_rejection_count += 1
            self._last_error_code = f"publish:{reason}"

    def _reject_input(self, reason: str) -> None:
        with self._lock:
            self._input_rejection_count += 1
            self._last_error_code = f"input:{reason}"

    def _offer_formal_event(self, decision: ScoredDecision) -> None:
        if not self._observer.offer(V2DecisionObservation(build_v2_decision_committed(decision), None)):
            with self._lock:
                self._observer_rejection_count += 1

    def _worker_name(self) -> str:
        return f"trader-v2-{self._strategy.value}"


__all__ = [
    "TomorrowV2Runtime",
    "TomorrowV2RuntimeDependencies",
    "TomorrowV2RuntimeStatus",
]
