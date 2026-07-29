"""Non-blocking production shadow orchestration for tomorrow v2."""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from trader.application.current_decisions import CurrentDecisionIndex
from trader.application.policy import RecommendationPolicy
from trader.application.ports.clock import Clock
from trader.application.ports.snapshots import (
    PublishedSnapshotReadPort,
    PublishedSnapshotWritePort,
    SnapshotStatusValue,
)
from trader.application.ports.tomorrow import TomorrowNativeInput, TomorrowNativeInputPort
from trader.application.tomorrow_events import TomorrowDecisionEventStream
from trader.application.tomorrow_freezing import TomorrowFreezeCoordinator
from trader.application.tomorrow_shadow import (
    TomorrowCutoverGate,
    TomorrowShadowObservation,
)
from trader.application.tomorrow_shadow_projection import (
    TomorrowShadowProjection,
    native_input_from_snapshot,
    project_tomorrow_input,
    project_tomorrow_snapshot,
)
from trader.application.tomorrow_views import (
    TomorrowDecisionQueries,
    TomorrowLiveQuote,
    TomorrowQuoteOverlay,
    TomorrowQuoteOverlayIndex,
    TomorrowRuntimeTelemetry,
)
from trader.domain.recommendation.models import LiveOverlay, RecommendationSnapshot, Strategy
from trader.domain.recommendation.tomorrow_fusion import DecisionEpoch

SHANGHAI = ZoneInfo("Asia/Shanghai")


class TomorrowShadowProcessor(Protocol):
    def process(self, snapshot: RecommendationSnapshot) -> bool: ...

    def process_native(self, native_input: TomorrowNativeInput) -> bool: ...


class _PublishedSnapshotIndex(PublishedSnapshotReadPort, PublishedSnapshotWritePort, Protocol):
    pass


@dataclass(frozen=True)
class TomorrowShadowDependencies:
    decisions: CurrentDecisionIndex
    quotes: TomorrowQuoteOverlayIndex
    events: TomorrowDecisionEventStream
    queries: TomorrowDecisionQueries
    freezer: TomorrowFreezeCoordinator
    gate: TomorrowCutoverGate
    clock: Clock


@dataclass(frozen=True)
class _NativeProjectionRecord:
    native_input: TomorrowNativeInput
    sequence: int
    local_version: str
    local_publish_seconds: float


class TomorrowShadowRuntime:
    """Publishes native v2 input and later observes the matching v1 baseline."""

    def __init__(
        self,
        policy: RecommendationPolicy,
        dependencies: TomorrowShadowDependencies,
    ) -> None:
        self._policy = policy
        self._decisions = dependencies.decisions
        self._quotes = dependencies.quotes
        self._events = dependencies.events
        self._queries = dependencies.queries
        self._freezer = dependencies.freezer
        self._gate = dependencies.gate
        self._clock = dependencies.clock
        self._lock = threading.RLock()
        self._native_records: dict[str, _NativeProjectionRecord] = {}
        self._decision_sequence = 0
        self._processed = 0
        self._native_processed = 0
        self._native_coalesced = 0
        self._native_superseded = 0
        self._baseline_fallbacks = 0
        self._baseline_superseded = 0
        self._failed = 0
        self._skipped_sealed = 0
        self._last_error = ""
        self._last_pipeline_latency_ms: float | None = None
        self._last_publish_latency_ms: float | None = None

    def process_native(self, native_input: TomorrowNativeInput) -> bool:
        started = time.perf_counter()
        with self._lock:
            if native_input.input_version in self._native_records:
                self._native_coalesced += 1
                return True
            current = self._decisions.latest()
            if (
                current is not None
                and current.trade_date == native_input.trade_date
                and current.observed_at > native_input.evaluated_at
            ):
                self._native_superseded += 1
                return True
        sequence = self._next_sequence()
        try:
            projection = project_tomorrow_input(
                native_input,
                self._policy,
                decision_sequence=sequence,
            )
            if not self._publish_decision(projection.local):
                with self._lock:
                    self._skipped_sealed += 1
                return True
            published_at = _clock_now(self._clock)
            record = _NativeProjectionRecord(
                native_input,
                sequence,
                projection.local.version,
                max(0.0, (published_at - projection.received_at).total_seconds()),
            )
            with self._lock:
                self._native_records.pop(projection.input_version, None)
                self._native_records[projection.input_version] = record
                while len(self._native_records) > 64:
                    self._native_records.pop(next(iter(self._native_records)))
                self._native_processed += 1
                self._last_pipeline_latency_ms = (time.perf_counter() - started) * 1000.0
                self._last_error = ""
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            with self._lock:
                self._failed += 1
                self._last_error = f"{type(exc).__name__}:{str(exc)[:240]}"
                self._last_pipeline_latency_ms = (time.perf_counter() - started) * 1000.0
            return False
        return True

    def process(self, snapshot: RecommendationSnapshot) -> bool:
        started = time.perf_counter()
        try:
            native_input = native_input_from_snapshot(snapshot)
            with self._lock:
                native_record = self._native_records.get(native_input.input_version)
            if native_record is None:
                current = self._decisions.latest()
                if (
                    current is not None
                    and current.trade_date == native_input.trade_date
                    and current.observed_at > native_input.evaluated_at
                ):
                    with self._lock:
                        self._baseline_superseded += 1
                    return True
                projection, local_publish_seconds = self._fallback_projection(snapshot)
                if projection is None:
                    return True
            else:
                projection = project_tomorrow_input(
                    native_record.native_input,
                    self._policy,
                    decision_sequence=native_record.sequence,
                    reviews=snapshot.replay_input.reviews if snapshot.replay_input is not None else {},
                )
                if projection.local.version != native_record.local_version:
                    raise RuntimeError("tomorrow native local identity changed before baseline observation")
                current = self._decisions.latest()
                if current is None or (
                    current.version not in {projection.local.version, getattr(projection.hybrid, "version", "")}
                    and current.sequence > native_record.sequence
                ):
                    with self._lock:
                        self._baseline_superseded += 1
                    return True
                local_publish_seconds = native_record.local_publish_seconds
            effective = self._publish_effective_projection(projection)
            if snapshot.frozen:
                freeze_result = self._freezer.freeze_scheduled()
                if freeze_result.status in {"frozen", "already_frozen"}:
                    self._events.publish_decision(self._queries.current())
            now = _clock_now(self._clock)
            frozen = self._decisions.frozen()
            observation = TomorrowShadowObservation(
                trade_date=effective.trade_date,
                observed_at=now,
                baseline_snapshot_id=snapshot.snapshot_id,
                decision_version=effective.version,
                input_version=projection.input_version,
                selected_codes_match=_selected_codes(snapshot) == _selected_codes(effective),
                filter_reasons_match=dict(snapshot.filter_reasons) == dict(effective.filter_reason_counts),
                local_publish_seconds=local_publish_seconds,
                decision_age_seconds=max(0.0, (now - projection.received_at).total_seconds()),
                deepseek_request_delta=0,
                resource_limits_passed=True,
                baseline_frozen=snapshot.frozen,
                v2_frozen=frozen is not None and frozen.trade_date == effective.trade_date,
                freeze_codes_match=(
                    frozen is not None
                    and frozen.trade_date == effective.trade_date
                    and _selected_codes(snapshot) == _selected_codes(frozen.decision)
                ),
            )
            self._gate.record(observation)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self._record_failure(snapshot, exc, started)
            return False
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        with self._lock:
            self._processed += 1
            self._last_pipeline_latency_ms = elapsed_ms
            self._last_error = ""
        return True

    def _fallback_projection(
        self,
        snapshot: RecommendationSnapshot,
    ) -> tuple[TomorrowShadowProjection | None, float]:
        sequence = self._next_sequence()
        projection = project_tomorrow_snapshot(
            snapshot,
            self._policy,
            decision_sequence=sequence,
        )
        if not self._publish_decision(projection.local):
            with self._lock:
                self._skipped_sealed += 1
            return None, 0.0
        local_published_at = _clock_now(self._clock)
        with self._lock:
            self._baseline_fallbacks += 1
        return projection, max(
            0.0,
            (local_published_at - projection.received_at).total_seconds(),
        )

    def _publish_effective_projection(self, projection: TomorrowShadowProjection) -> DecisionEpoch:
        local = projection.local
        hybrid = projection.hybrid
        current = self._decisions.latest()
        if current is None:
            if not self._publish_decision(local):
                return local
            current = local
        if current.version == local.version and hybrid is not None:
            if self._publish_decision(hybrid):
                return hybrid
        if hybrid is not None and current.version == hybrid.version:
            return hybrid
        if current.version != local.version:
            raise RuntimeError("tomorrow baseline does not match the current native decision")
        return local

    def publish_overlay(self, overlay: LiveOverlay) -> bool:
        if overlay.strategy is not Strategy.TOMORROW:
            return False
        decision = self._decisions.latest()
        if decision is None or decision.trade_date.isoformat() != overlay.trade_date:
            return False
        selected = {item.code for item in decision.entries if item.selected}
        quotes = tuple(
            TomorrowLiveQuote(
                code=code,
                price=quote.price,
                pct_change=quote.pct_change,
                source=quote.source,
                source_time=_shanghai(quote.source_time),
                data_version=quote.data_version,
            )
            for code, quote in sorted(overlay.quotes.items())
            if code in selected
        )
        observed_at = _shanghai(overlay.observed_at)
        current = self._quotes.latest(decision.version)
        projected = TomorrowQuoteOverlay(
            decision_version=decision.version,
            version=f"shadow:{overlay.version}",
            observed_at=observed_at,
            quotes=quotes,
        )
        result = self._quotes.publish(
            projected,
            expected_overlay_version=current.version if current is not None else None,
        )
        if not result.accepted:
            return False
        self._events.publish_overlay(projected)
        return True

    def telemetry(self) -> TomorrowRuntimeTelemetry:
        with self._lock:
            failures = (self._last_error,) if self._last_error else ()
            return TomorrowRuntimeTelemetry(
                pipeline_latency_ms=self._last_pipeline_latency_ms,
                publish_latency_ms=self._last_publish_latency_ms,
                recent_failures=failures,
            )

    def status(self) -> Mapping[str, object]:
        with self._lock:
            runtime = {
                "processed": self._processed,
                "native_processed": self._native_processed,
                "native_coalesced": self._native_coalesced,
                "native_superseded": self._native_superseded,
                "baseline_fallbacks": self._baseline_fallbacks,
                "baseline_superseded": self._baseline_superseded,
                "failed": self._failed,
                "skipped_sealed": self._skipped_sealed,
                "last_error": self._last_error,
                "pipeline_latency_ms": self._last_pipeline_latency_ms,
                "publish_latency_ms": self._last_publish_latency_ms,
            }
        return {**runtime, "cutover_gate": asdict(self._gate.status())}

    def _publish_decision(self, decision: DecisionEpoch) -> bool:
        started = time.perf_counter()
        current = self._decisions.latest()
        result = self._decisions.publish(
            decision,
            expected_current_version=current.version if current is not None else None,
        )
        if not result.accepted:
            if result.reason == "freeze_sealed":
                return False
            raise RuntimeError(f"tomorrow shadow decision rejected: {result.reason}")
        view = self._queries.current()
        if view.status != "ready" or view.decision_version != decision.version:
            raise RuntimeError("tomorrow shadow query did not expose the accepted decision")
        self._events.publish_decision(view)
        with self._lock:
            self._last_publish_latency_ms = (time.perf_counter() - started) * 1000.0
        return True

    def _next_sequence(self) -> int:
        with self._lock:
            current = self._decisions.latest()
            floor = current.sequence + 1 if current is not None else 0
            sequence = max(self._decision_sequence, floor)
            self._decision_sequence = sequence + 2
            return sequence

    def _record_failure(
        self,
        snapshot: RecommendationSnapshot,
        error: Exception,
        started: float,
    ) -> None:
        now = _clock_now(self._clock)
        reason = f"{type(error).__name__}:{str(error)[:240]}"
        self._gate.record(
            TomorrowShadowObservation(
                trade_date=_snapshot_trade_date(snapshot),
                observed_at=now,
                baseline_snapshot_id=snapshot.snapshot_id,
                decision_version="unavailable",
                input_version=snapshot.data_version or "unavailable",
                selected_codes_match=False,
                filter_reasons_match=False,
                local_publish_seconds=max(0.0, time.perf_counter() - started),
                decision_age_seconds=0.0,
                deepseek_request_delta=0,
                resource_limits_passed=True,
                baseline_frozen=snapshot.frozen,
                v2_frozen=False,
                freeze_codes_match=False,
                processing_error="shadow_projection_failed",
            )
        )
        with self._lock:
            self._failed += 1
            self._last_error = reason


class TomorrowShadowWorker(TomorrowNativeInputPort):
    """One stoppable latest-wins worker; it never blocks v1 publication."""

    def __init__(
        self,
        processor: TomorrowShadowProcessor,
        *,
        thread_name: str = "tomorrow-v2-shadow",
    ) -> None:
        self._processor = processor
        self._thread_name = thread_name
        self._condition = threading.Condition()
        self._thread: threading.Thread | None = None
        self._pending: RecommendationSnapshot | TomorrowNativeInput | None = None
        self._stopping = False
        self._offered = 0
        self._native_offered = 0
        self._baseline_offered = 0
        self._replaced = 0
        self._completed = 0
        self._failed = 0
        self._last_error = ""

    def start(self) -> bool:
        with self._condition:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stopping = False
            self._thread = threading.Thread(
                target=self._run,
                name=self._thread_name,
                daemon=False,
            )
            self._thread.start()
            return True

    def offer(self, snapshot: RecommendationSnapshot) -> bool:
        if snapshot.strategy is not Strategy.TOMORROW or snapshot.replay_input is None:
            return False
        return self._offer(snapshot, native=False)

    def offer_native(self, native_input: TomorrowNativeInput) -> bool:
        return self._offer(native_input, native=True)

    def _offer(
        self,
        payload: RecommendationSnapshot | TomorrowNativeInput,
        *,
        native: bool,
    ) -> bool:
        with self._condition:
            if self._stopping:
                return False
            self._offered += 1
            if native:
                self._native_offered += 1
            else:
                self._baseline_offered += 1
            if self._pending is not None:
                self._replaced += 1
            self._pending = payload
            self._condition.notify()
            return True

    def stop(self, *, wait: bool, timeout_seconds: float | None = None) -> None:
        with self._condition:
            self._stopping = True
            self._pending = None
            thread = self._thread
            self._condition.notify_all()
        if wait and thread is not None:
            thread.join(timeout_seconds)

    def status(self) -> Mapping[str, object]:
        with self._condition:
            return {
                "running": self._thread is not None and self._thread.is_alive(),
                "pending": self._pending is not None,
                "offered": self._offered,
                "native_offered": self._native_offered,
                "baseline_offered": self._baseline_offered,
                "replaced": self._replaced,
                "completed": self._completed,
                "failed": self._failed,
                "last_error": self._last_error,
                "capacity": 1,
            }

    def _run(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._stopping or self._pending is not None)
                if self._stopping:
                    return
                payload = self._pending
                self._pending = None
            if payload is None:
                continue
            try:
                completed = (
                    self._processor.process_native(payload)
                    if isinstance(payload, TomorrowNativeInput)
                    else self._processor.process(payload)
                )
            except Exception as exc:
                completed = False
                worker_error = f"worker_exception:{type(exc).__name__}"
            else:
                worker_error = ""
            with self._condition:
                if completed:
                    self._completed += 1
                    self._last_error = ""
                else:
                    self._failed += 1
                    self._last_error = worker_error or "shadow_processing_failed"


class ShadowObservingSnapshotIndex(PublishedSnapshotWritePort):
    """Delegates v1 P6 admission and asynchronously mirrors accepted tomorrow input."""

    def __init__(
        self,
        delegate: _PublishedSnapshotIndex,
        worker: TomorrowShadowWorker,
        runtime: TomorrowShadowRuntime,
    ) -> None:
        self._delegate = delegate
        self._worker = worker
        self._runtime = runtime

    def publish(self, snapshot: RecommendationSnapshot) -> bool:
        accepted = self._delegate.publish(snapshot)
        if accepted:
            self._worker.offer(snapshot)
        return accepted

    def publish_overlay(self, overlay: LiveOverlay) -> None:
        self._delegate.publish_overlay(overlay)
        try:
            self._runtime.publish_overlay(overlay)
        except (RuntimeError, TypeError, ValueError):
            return

    def status(self) -> Mapping[str, SnapshotStatusValue]:
        return {
            **self._delegate.status(),
            "tomorrow_shadow": {
                **self._worker.status(),
                **self._runtime.status(),
            },
        }


def _selected_codes(snapshot: RecommendationSnapshot | DecisionEpoch) -> tuple[str, ...]:
    if isinstance(snapshot, DecisionEpoch):
        selected = sorted((item for item in snapshot.entries if item.selected), key=lambda item: item.rank)
        return tuple(item.code for item in selected)
    return tuple(item.features.quote.code for item in snapshot.recommendations)


def _clock_now(clock: Clock) -> datetime:
    return _shanghai(clock.now())


def _snapshot_trade_date(snapshot: RecommendationSnapshot) -> date:
    try:
        return date.fromisoformat(snapshot.trade_date)
    except ValueError:
        return _shanghai(snapshot.published_at).date()


def _shanghai(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("tomorrow shadow time must be timezone-aware")
    return value.astimezone(SHANGHAI)


__all__ = [
    "ShadowObservingSnapshotIndex",
    "TomorrowShadowDependencies",
    "TomorrowShadowProcessor",
    "TomorrowShadowRuntime",
    "TomorrowShadowWorker",
]
