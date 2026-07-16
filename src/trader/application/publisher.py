"""In-process published-version stream with bounded subscriber queues."""

from __future__ import annotations

import json
import queue
import threading
from collections import deque
from collections.abc import Iterator, Mapping
from dataclasses import dataclass

from trader.domain.models import RecommendationSnapshot


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
    def __init__(self, *, history_size: int, client_queue_size: int, maximum_subscribers: int = 64) -> None:
        self._history: deque[PublishedEvent] = deque(maxlen=max(1, history_size))
        self._client_queue_size = max(1, client_queue_size)
        self._maximum_subscribers = max(1, maximum_subscribers)
        self._lock = threading.Lock()
        self._sequence = 0
        self._subscribers: set[queue.Queue[PublishedEvent]] = set()
        self._dropped_subscribers = 0

    def publish(self, snapshot: RecommendationSnapshot) -> PublishedEvent:
        event = self._new_event(
            "recommendations",
            {
                "snapshot_id": snapshot.snapshot_id,
                "strategy": snapshot.strategy.value,
                "published_at": snapshot.published_at.isoformat(),
                "data_version": snapshot.data_version,
                "fusion_mode": snapshot.fusion_mode.value,
                "frozen": snapshot.frozen,
            },
        )
        return event

    def resync(self, reason: str) -> PublishedEvent:
        return self._new_event("resync_required", {"reason": reason})

    def events_after(self, sequence: int) -> tuple[PublishedEvent, ...] | None:
        with self._lock:
            return self._events_after_locked(sequence)

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

    def status(self) -> dict[str, int]:
        with self._lock:
            return {
                "last_sequence": self._sequence,
                "history_size": len(self._history),
                "subscribers": len(self._subscribers),
                "maximum_subscribers": self._maximum_subscribers,
                "dropped_subscribers": self._dropped_subscribers,
            }

    def _new_event(self, event_type: str, data: Mapping[str, object]) -> PublishedEvent:
        with self._lock:
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


__all__ = [
    "PublishedEvent",
    "SnapshotPublisher",
    "SubscriberLimitError",
    "Subscription",
    "encode_sse",
]
