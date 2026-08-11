"""Production Today V2 native-input lane, freeze control, and quote overlay."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from trader.application.decision_core import UnifiedDecisionIndex
from trader.application.decision_events import V2DecisionCommitted, build_v2_decision_committed
from trader.application.decision_observers import DecisionObserverRuntime
from trader.application.policy import RecommendationPolicy
from trader.application.ports.clock import Clock
from trader.application.ports.reviews import DeepSeekReviewUnavailableError, TomorrowDeepSeekReviewPort
from trader.application.ports.tomorrow import TodayNativeInput
from trader.application.shutdown import ShutdownDeadline, ShutdownStep
from trader.application.today_v2_freezing import TodayV2FreezeCoordinator
from trader.application.today_v2_projection import (
    TodayV2LocalProjection,
    build_today_v2_hybrid,
    build_today_v2_local,
    validate_review_manifests,
)
from trader.application.tomorrow_v2_freezing import V2FreezeOperationResult
from trader.application.v2_lifecycle import LatestWinsStatus, LatestWinsWorker
from trader.domain.market.models import MarketQuote
from trader.domain.recommendation.decision_identity import DecisionOverlay, OverlayQuote, ScoredDecision, identity_codes
from trader.domain.recommendation.models import Strategy

SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class TodayV2RuntimeStatus:
    worker: LatestWinsStatus
    local_publish_count: int
    hybrid_publish_count: int
    publish_rejection_count: int
    review_failure_count: int
    review_late_count: int
    observer_rejection_count: int
    input_rejection_count: int
    freeze_status: str
    last_error_code: str


@dataclass(frozen=True)
class TodayV2RuntimeDependencies:
    reviewer: TomorrowDeepSeekReviewPort | None
    index: UnifiedDecisionIndex
    observer: DecisionObserverRuntime
    freezer: TodayV2FreezeCoordinator
    clock: Clock
    publish_overlay: Callable[[DecisionOverlay], object] = lambda _overlay: None


class TodayV2Runtime:
    """One latest-wins Today producer that permanently closes at 11:20."""

    def __init__(
        self,
        policy: RecommendationPolicy | None,
        dependencies: TodayV2RuntimeDependencies,
    ) -> None:
        self._policy = policy
        self._reviewer = dependencies.reviewer
        self._index = dependencies.index
        self._observer = dependencies.observer
        self._freezer = dependencies.freezer
        self._clock = dependencies.clock
        self._publish_overlay_event = dependencies.publish_overlay
        self._lock = threading.RLock()
        self._sequence = 1
        self._local_publish_count = 0
        self._hybrid_publish_count = 0
        self._publish_rejection_count = 0
        self._review_failure_count = 0
        self._review_late_count = 0
        self._observer_rejection_count = 0
        self._input_rejection_count = 0
        self._freeze_status = "not_attempted"
        self._last_error_code = ""
        self._worker = LatestWinsWorker(
            "trader-v2-today",
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
        formal = self._index.snapshot(Strategy.TODAY).formal
        if formal is not None:
            self._observer.offer(build_v2_decision_committed(formal.decision))
        return started

    def initialize(self) -> None:
        result = self._freezer.initialize()
        with self._lock:
            self._freeze_status = result.status
            if result.status == "persistence_failed":
                self._last_error_code = "freeze:persistence_failed"
        if result.record is not None and self._observer.status().accepting:
            self._offer_formal_event(result.record.decision)

    def offer_native(self, native_input: TodayNativeInput) -> bool:
        boundary = datetime.combine(native_input.trade_date, time(11, 20), tzinfo=native_input.evaluated_at.tzinfo)
        if native_input.evaluated_at >= boundary or self._index.is_closed(Strategy.TODAY, native_input.trade_date):
            self._reject_input("freeze_closed")
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
            return ShutdownStep(name="trader-v2-today-close", completed=True, timed_out=False)
        worker = self._worker.stop(deadline=shared)
        observer = self._observer.stop(deadline=shared)
        completed = worker.completed and observer.completed
        return ShutdownStep(
            name="trader-v2-today",
            completed=completed,
            timed_out=not completed and shared.expired,
            cancelled_count=worker.cancelled_count + observer.cancelled_count,
            detail="" if completed else "Today V2 runtime remains active",
        )

    def on_clock(self, at: datetime) -> V2FreezeOperationResult | None:
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("Today V2 control time must be timezone-aware")
        local = at.astimezone(self._clock.now().tzinfo)
        boundary = datetime.combine(local.date(), time(11, 20), tzinfo=local.tzinfo)
        if local < boundary:
            return None
        result = self._freezer.freeze_scheduled()
        with self._lock:
            self._freeze_status = result.status
        if result.status == "frozen" and result.record is not None:
            self._offer_formal_event(result.record.decision)
        return result

    def overlay_codes(self, trade_date: date) -> tuple[str, ...]:
        formal = self._index.snapshot(Strategy.TODAY).formal
        if formal is None or formal.trade_date != trade_date:
            return ()
        return tuple(sorted(identity_codes(formal.decision)))

    def publish_overlay(
        self,
        quotes: Mapping[str, MarketQuote],
        *,
        observed_at: datetime,
        closing: bool,
    ) -> bool:
        del closing
        observed_at = _shanghai(observed_at)
        snapshot = self._index.snapshot(Strategy.TODAY)
        formal = snapshot.formal
        if formal is None or formal.trade_date != observed_at.date():
            return False
        allowed = identity_codes(formal.decision)
        if not quotes or not set(quotes).issubset(allowed):
            return False
        existing_quotes = {item.code: item for item in snapshot.overlay.quotes} if snapshot.overlay is not None else {}
        for code, quote in quotes.items():
            source_time = _shanghai(quote.source_time)
            if quote.code != code or quote.price is None or quote.price <= 0 or source_time > observed_at:
                return False
            existing_quotes[code] = OverlayQuote(
                code,
                quote.price,
                quote.pct_change,
                quote.source,
                source_time,
                quote.data_version,
            )
        overlay = DecisionOverlay(
            Strategy.TODAY,
            formal.trade_date,
            formal.decision.version,
            observed_at,
            tuple(existing_quotes.values()),
        )
        expected = snapshot.overlay.version if snapshot.overlay is not None else None
        result = self._index.publish_overlay(overlay, expected_version=expected)
        if result.accepted:
            self._publish_overlay_event(overlay)
        return result.accepted

    def status(self) -> TodayV2RuntimeStatus:
        with self._lock:
            return TodayV2RuntimeStatus(
                self._worker.status(),
                self._local_publish_count,
                self._hybrid_publish_count,
                self._publish_rejection_count,
                self._review_failure_count,
                self._review_late_count,
                self._observer_rejection_count,
                self._input_rejection_count,
                self._freeze_status,
                self._last_error_code,
            )

    def _process(self, native_input: TodayNativeInput) -> None:
        if self._freeze_if_boundary_reached(native_input):
            self._reject_input("freeze_closed")
            return
        if self._policy is None:
            self._reject_input("policy_unavailable")
            return
        projection = build_today_v2_local(native_input, self._policy, sequence=self._next_sequence())
        if not projection.input_quality.publishable:
            self._reject_input(projection.input_quality.status)
            return
        if self._freeze_if_boundary_reached(native_input):
            self._reject_input("freeze_closed")
            return
        local = projection.local
        expected = self._index.snapshot(Strategy.TODAY).current
        published = self._index.publish(local, expected_version=expected.version if expected is not None else None)
        if not published.accepted:
            self._reject_publish(published.reason)
            return
        self._record_publish(published.event, hybrid=False)
        self._try_hybrid_upgrade(native_input, projection, local)

    def _try_hybrid_upgrade(
        self,
        native_input: TodayNativeInput,
        projection: TodayV2LocalProjection,
        local: ScoredDecision,
    ) -> None:
        submit_cutoff = datetime.combine(native_input.trade_date, time(11, 18), tzinfo=native_input.evaluated_at.tzinfo)
        deadline = datetime.combine(native_input.trade_date, time(11, 20), tzinfo=native_input.evaluated_at.tzinfo)
        if native_input.evaluated_at >= submit_cutoff or self._clock.now() >= submit_cutoff:
            with self._lock:
                self._review_late_count += 1
            return
        if not projection.review_candidates or self._reviewer is None or self._policy is None:
            return
        expected_manifests = {
            candidate.code: self._reviewer.evidence_manifest_hash(candidate.features)
            for candidate in projection.review_candidates
        }
        try:
            reviews = dict(
                self._reviewer.review(
                    Strategy.TODAY,
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
        if self._clock.now() >= deadline:
            self.on_clock(self._clock.now())
            with self._lock:
                self._review_late_count += 1
            return
        if not validate_review_manifests(projection, reviews, expected_manifests):
            with self._lock:
                self._review_failure_count += 1
                self._last_error_code = "deepseek_identity_rejected"
            return
        hybrid = build_today_v2_hybrid(projection, self._policy, reviews, review_deadline=deadline)
        if hybrid is None:
            with self._lock:
                self._review_late_count += int(any(review.completed_at >= deadline for review in reviews.values()))
            return
        self._publish_hybrid(hybrid, expected_version=local.version)

    def _publish_hybrid(self, hybrid: ScoredDecision, *, expected_version: str) -> None:
        upgraded = self._index.publish(hybrid, expected_version=expected_version)
        if not upgraded.accepted:
            self._reject_publish(upgraded.reason)
            return
        self._record_publish(upgraded.event, hybrid=True)

    def _freeze_if_boundary_reached(self, native_input: TodayNativeInput) -> bool:
        boundary = datetime.combine(native_input.trade_date, time(11, 20), tzinfo=native_input.evaluated_at.tzinfo)
        now = self._clock.now()
        if now < boundary:
            return False
        self.on_clock(now)
        return True

    def _next_sequence(self) -> int:
        with self._lock:
            sequence = self._sequence
            self._sequence += 2
            return sequence

    def _record_publish(self, event: V2DecisionCommitted | None, *, hybrid: bool) -> None:
        with self._lock:
            self._hybrid_publish_count += int(hybrid)
            self._local_publish_count += int(not hybrid)
        if event is not None and not self._observer.offer(event):
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
        if not self._observer.offer(build_v2_decision_committed(decision)):
            with self._lock:
                self._observer_rejection_count += 1


def _shanghai(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Today V2 overlay time must be timezone-aware")
    return value.astimezone(SHANGHAI)


__all__ = ["TodayV2Runtime", "TodayV2RuntimeDependencies", "TodayV2RuntimeStatus"]
