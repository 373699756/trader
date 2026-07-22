"""In-process published-version stream with bounded subscriber queues."""

from __future__ import annotations

import json
import math
import queue
import threading
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

from trader.application.delivery_patch import overlay_patch, snapshot_patch
from trader.domain.recommendation.models import (
    LiveOverlay,
    RecommendationSnapshot,
)


@dataclass(frozen=True)
class PublishedEvent:
    sequence: int
    event_type: str
    data: Mapping[str, object]


@dataclass(frozen=True)
class Subscription:
    queue: queue.Queue[PublishedEvent]
    replay: tuple[PublishedEvent, ...] | None


class SubscriberLimitError(RuntimeError):
    pass


class SnapshotPublisher:
    def __init__(
        self,
        *,
        history_size: int,
        client_queue_size: int,
        maximum_subscribers: int = 64,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._history: deque[PublishedEvent] = deque(maxlen=max(1, history_size))
        self._client_queue_size = max(1, client_queue_size)
        self._maximum_subscribers = max(1, maximum_subscribers)
        self._now = now
        self._lock = threading.Lock()
        self._sequence = 0
        self._subscribers: set[queue.Queue[PublishedEvent]] = set()
        self._dropped_subscribers = 0
        self._publish_latencies: deque[float] = deque(maxlen=512)
        self._today_score_latencies: deque[float] = deque(maxlen=4096)
        self._snapshot_ids: dict[str, str] = {}

    def publish(self, snapshot: RecommendationSnapshot) -> PublishedEvent:
        emitted_at = self._now()
        with self._lock:
            base_snapshot_id = self._snapshot_ids.get(snapshot.strategy.value)
            self._snapshot_ids[snapshot.strategy.value] = snapshot.snapshot_id
            event = self._new_event_locked(
                "recommendation_patch",
                snapshot_patch(snapshot, base_snapshot_id=base_snapshot_id),
            )
            self._publish_latencies.append(max(0.0, (emitted_at - snapshot.published_at).total_seconds()))
            if snapshot.strategy.value == "today":
                self._today_score_latencies.extend(
                    max(0.0, (emitted_at - item.features.quote.source_time).total_seconds())
                    for item in snapshot.recommendations
                )
        return event

    def resync(self, reason: str) -> PublishedEvent:
        return self._new_event("resync_required", {"reason": reason})

    def publish_overlay(self, overlay: LiveOverlay) -> PublishedEvent:
        emitted_at = self._now()
        event = self._new_event("overlay_patch", overlay_patch(overlay))
        with self._lock:
            self._publish_latencies.append(max(0.0, (emitted_at - overlay.observed_at).total_seconds()))
        return event

    def events_after(self, sequence: int) -> tuple[PublishedEvent, ...] | None:
        with self._lock:
            return self._events_after_locked(sequence)

    def last_sequence(self) -> int:
        with self._lock:
            return self._sequence

    def subscribe(self) -> queue.Queue[PublishedEvent]:
        return self.open_subscription(self._sequence).queue

    def open_subscription(self, after_sequence: int) -> Subscription:
        subscriber: queue.Queue[PublishedEvent] = queue.Queue(maxsize=self._client_queue_size)
        with self._lock:
            if len(self._subscribers) >= self._maximum_subscribers:
                raise SubscriberLimitError("SSE subscriber limit reached")
            replay = self._events_after_locked(after_sequence)
            self._subscribers.add(subscriber)
        return Subscription(subscriber, replay)

    def unsubscribe(self, subscriber: queue.Queue[PublishedEvent]) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    def is_subscribed(self, subscriber: queue.Queue[PublishedEvent]) -> bool:
        with self._lock:
            return subscriber in self._subscribers

    def stream(
        self, subscriber: queue.Queue[PublishedEvent], *, timeout_seconds: float
    ) -> Iterator[PublishedEvent | None]:
        while True:
            try:
                yield subscriber.get(timeout=timeout_seconds)
            except queue.Empty:
                yield None

    def status(self) -> dict[str, object]:
        with self._lock:
            return {
                "last_sequence": self._sequence,
                "history_size": len(self._history),
                "subscribers": len(self._subscribers),
                "maximum_subscribers": self._maximum_subscribers,
                "dropped_subscribers": self._dropped_subscribers,
                "sse_publish_latency": _latency_summary(self._publish_latencies, target_seconds=2.0),
                "today_score_publish_latency": _latency_summary(
                    self._today_score_latencies,
                    target_seconds=15.0,
                ),
            }

    def _new_event(self, event_type: str, data: Mapping[str, object]) -> PublishedEvent:
        with self._lock:
            return self._new_event_locked(event_type, data)

    def _new_event_locked(self, event_type: str, data: Mapping[str, object]) -> PublishedEvent:
        self._sequence += 1
        event = PublishedEvent(self._sequence, event_type, dict(data))
        self._history.append(event)
        stale_subscribers: list[queue.Queue[PublishedEvent]] = []
        for subscriber in self._subscribers:
            try:
                subscriber.put_nowait(event)
            except queue.Full:
                stale_subscribers.append(subscriber)
        for subscriber in stale_subscribers:
            self._subscribers.discard(subscriber)
            self._dropped_subscribers += 1
        return event

    def _events_after_locked(self, sequence: int) -> tuple[PublishedEvent, ...] | None:
        if sequence > self._sequence:
            return None
        if self._history and sequence < self._history[0].sequence - 1:
            return None
        return tuple(event for event in self._history if event.sequence > sequence)


def encode_sse(event: PublishedEvent) -> str:
    body = json.dumps(event.data, ensure_ascii=False, separators=(",", ":"))
    return f"id: {event.sequence}\nevent: {event.event_type}\ndata: {body}\n\n"


def _latency_summary(values: deque[float], *, target_seconds: float) -> dict[str, object]:
    if not values:
        return {
            "sample_count": 0,
            "p50_seconds": None,
            "p95_seconds": None,
            "maximum_seconds": None,
            "meets_target": None,
        }
    ordered = sorted(values)
    p50 = ordered[max(0, math.ceil(len(ordered) * 0.50) - 1)]
    p95 = ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]
    return {
        "sample_count": len(ordered),
        "p50_seconds": round(p50, 3),
        "p95_seconds": round(p95, 3),
        "maximum_seconds": round(ordered[-1], 3),
        "meets_target": p95 <= target_seconds,
    }


__all__ = [
    "PublishedEvent",
    "SnapshotPublisher",
    "SubscriberLimitError",
    "Subscription",
    "encode_sse",
]
