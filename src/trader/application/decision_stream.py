"""Bounded non-blocking event publication for all V2 strategies."""

from __future__ import annotations

import hashlib
import queue
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from trader.application.decision_coverage import DecisionCoverage, scored_decision_coverage
from trader.application.decision_events import V2DecisionCommitted
from trader.domain.recommendation.decision_identity import (
    DecisionItem,
    DecisionOverlay,
    DecisionQuote,
    LongProjection,
    SelectionDiagnostics,
)
from trader.domain.recommendation.models import Strategy

ResyncReason = Literal[
    "cursor_ahead",
    "cursor_expired",
    "cursor_gap",
    "slow_subscriber",
    "base_mismatch",
    "schema_mismatch",
    "identity_mismatch",
]
EventType = Literal["decision", "overlay", "resync_required"]


@dataclass(frozen=True)
class DecisionReplacementPatch:
    projection_version: str
    observed_at: datetime
    strategy_version: str
    input_versions: tuple[tuple[str, str], ...]
    fusion_mode: str
    coverage: DecisionCoverage
    selection_diagnostics: SelectionDiagnostics | None
    degraded_reasons: tuple[str, ...]
    items: tuple[DecisionItem, ...]


@dataclass(frozen=True)
class DecisionEventPayload:
    strategy: Strategy
    trade_date: str
    version: str
    content_hash: str
    stage: str
    replacement: DecisionReplacementPatch | None


@dataclass(frozen=True)
class OverlayEventPayload:
    strategy: Strategy
    trade_date: str
    version: str
    parent_version: str
    content_hash: str
    projection_version: str
    quotes: tuple[DecisionQuote, ...]


@dataclass(frozen=True)
class ResyncEventPayload:
    reason: ResyncReason


UnifiedEventPayload = DecisionEventPayload | OverlayEventPayload | ResyncEventPayload


@dataclass(frozen=True)
class UnifiedPublishedEvent:
    sequence: int
    event_type: EventType
    payload: UnifiedEventPayload


@dataclass(frozen=True)
class UnifiedSubscription:
    queue: queue.Queue[UnifiedPublishedEvent]
    replay: tuple[UnifiedPublishedEvent, ...]
    server_sequence_at_open: int
    resync_reason: ResyncReason | None


@dataclass(frozen=True)
class UnifiedEventStreamStatus:
    sequence: int
    history_size: int
    subscriber_count: int
    slow_subscriber_drops: int


class UnifiedSubscriberLimitError(RuntimeError):
    """The event stream has no free bounded subscriber slot."""


class UnifiedDecisionEventStream:
    def __init__(
        self,
        *,
        history_size: int = 256,
        client_queue_size: int = 16,
        subscriber_limit: int = 32,
    ) -> None:
        if min(history_size, client_queue_size, subscriber_limit) < 1:
            raise ValueError("decision event stream bounds must be positive")
        self._lock = threading.RLock()
        self._history: deque[UnifiedPublishedEvent] = deque(maxlen=history_size)
        self._client_queue_size = client_queue_size
        self._subscriber_limit = subscriber_limit
        self._subscribers: set[queue.Queue[UnifiedPublishedEvent]] = set()
        self._sequence = 0
        self._slow_subscriber_drops = 0

    def publish_committed(self, event: V2DecisionCommitted) -> UnifiedPublishedEvent:
        decision = event.projection
        replacement = None
        if decision is not None:
            replacement = DecisionReplacementPatch(
                event.projection_version or event.decision_hash,
                decision.observed_at,
                decision.strategy_version,
                decision.input_versions,
                decision.stage,
                scored_decision_coverage(decision),
                decision.selection_diagnostics,
                decision.degraded_reasons,
                tuple(sorted((item for item in decision.items if item.selected), key=lambda item: item.rank)),
            )
        payload = DecisionEventPayload(
            event.strategy,
            event.trade_date.isoformat(),
            event.decision_version,
            event.decision_hash,
            event.stage,
            replacement,
        )
        with self._lock:
            return self._publish_locked("decision", payload)

    def publish_projection(self, projection: LongProjection) -> UnifiedPublishedEvent:
        payload = DecisionEventPayload(
            Strategy.LONG,
            projection.trade_date.isoformat(),
            projection.version,
            projection.content_hash,
            "current",
            None,
        )
        with self._lock:
            return self._publish_locked("decision", payload)

    def publish_overlay(
        self,
        overlay: DecisionOverlay,
        *,
        parent_content_hash: str,
    ) -> UnifiedPublishedEvent:
        projection_version = hashlib.sha256(f"{parent_content_hash}|{overlay.content_hash}".encode()).hexdigest()
        payload = OverlayEventPayload(
            overlay.strategy,
            overlay.trade_date.isoformat(),
            overlay.version,
            overlay.parent_version,
            overlay.content_hash,
            projection_version,
            overlay.quotes,
        )
        with self._lock:
            return self._publish_locked("overlay", payload)

    def publish_resync(self, reason: ResyncReason) -> UnifiedPublishedEvent:
        with self._lock:
            return self._publish_locked("resync_required", ResyncEventPayload(reason))

    def open_subscription(self, after_sequence: int | None) -> UnifiedSubscription:
        if after_sequence is not None and after_sequence < 0:
            raise ValueError("decision event cursor cannot be negative")
        subscriber: queue.Queue[UnifiedPublishedEvent] = queue.Queue(maxsize=self._client_queue_size)
        with self._lock:
            if len(self._subscribers) >= self._subscriber_limit:
                raise UnifiedSubscriberLimitError("decision event subscriber limit reached")
            sequence = self._sequence
            cursor = sequence if after_sequence is None else after_sequence
            replay, reason = self._replay_locked(cursor)
            self._subscribers.add(subscriber)
            return UnifiedSubscription(subscriber, replay, sequence, reason)

    def unsubscribe(self, subscriber: queue.Queue[UnifiedPublishedEvent]) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    def is_subscribed(self, subscriber: queue.Queue[UnifiedPublishedEvent]) -> bool:
        with self._lock:
            return subscriber in self._subscribers

    def last_sequence(self) -> int:
        with self._lock:
            return self._sequence

    def status(self) -> UnifiedEventStreamStatus:
        with self._lock:
            return UnifiedEventStreamStatus(
                self._sequence,
                len(self._history),
                len(self._subscribers),
                self._slow_subscriber_drops,
            )

    def _publish_locked(self, event_type: EventType, payload: UnifiedEventPayload) -> UnifiedPublishedEvent:
        self._sequence += 1
        event = UnifiedPublishedEvent(self._sequence, event_type, payload)
        self._history.append(event)
        dropped: list[queue.Queue[UnifiedPublishedEvent]] = []
        for subscriber in self._subscribers:
            try:
                subscriber.put_nowait(event)
            except queue.Full:
                dropped.append(subscriber)
        for subscriber in dropped:
            self._subscribers.discard(subscriber)
        self._slow_subscriber_drops += len(dropped)
        return event

    def _replay_locked(
        self,
        after_sequence: int,
    ) -> tuple[tuple[UnifiedPublishedEvent, ...], ResyncReason | None]:
        if after_sequence > self._sequence:
            return (), "cursor_ahead"
        if after_sequence == self._sequence:
            return (), None
        if not self._history or after_sequence < self._history[0].sequence - 1:
            return (), "cursor_expired"
        replay = tuple(event for event in self._history if event.sequence > after_sequence)
        if tuple(event.sequence for event in replay) != tuple(range(after_sequence + 1, self._sequence + 1)):
            return (), "cursor_gap"
        return replay, None


__all__ = [
    "DecisionEventPayload",
    "DecisionReplacementPatch",
    "OverlayEventPayload",
    "ResyncEventPayload",
    "ResyncReason",
    "UnifiedDecisionEventStream",
    "UnifiedEventStreamStatus",
    "UnifiedPublishedEvent",
    "UnifiedSubscriberLimitError",
    "UnifiedSubscription",
]
