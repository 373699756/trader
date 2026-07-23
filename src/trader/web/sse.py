"""Bounded SSE response construction and cursor recovery."""

from __future__ import annotations

import queue
from collections.abc import Iterator

from flask import Response, stream_with_context

from trader.application.publisher import PublishedEvent, SnapshotPublisher, SubscriberLimitError, encode_sse


def event_stream_response(
    publisher: SnapshotPublisher,
    *,
    after_sequence: int,
    heartbeat_seconds: float,
) -> Response:
    try:
        subscription = publisher.open_subscription(after_sequence)
    except SubscriberLimitError:
        raise

    def generate() -> Iterator[str]:
        subscriber = subscription.queue
        try:
            yield ": connected\n\n"
            if subscription.replay is None:
                reason = "cursor_ahead" if after_sequence > subscription.server_sequence_at_open else "cursor_expired"
                yield encode_sse(_resync_event(publisher, reason))
            else:
                for event in subscription.replay:
                    yield encode_sse(event)
            while True:
                if not publisher.is_subscribed(subscriber):
                    yield encode_sse(_resync_event(publisher, "slow_subscriber"))
                    return
                try:
                    event = subscriber.get(timeout=max(1.0, heartbeat_seconds))
                except queue.Empty:
                    yield ": heartbeat\n\n"
                    continue
                yield encode_sse(event)
        finally:
            publisher.unsubscribe(subscriber)

    response = Response(stream_with_context(generate()), mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-cache, no-transform"
    response.headers["X-Accel-Buffering"] = "no"
    response.headers["Connection"] = "keep-alive"
    return response


def _resync_event(publisher: SnapshotPublisher, reason: str) -> PublishedEvent:
    sequence = publisher.last_sequence()
    return PublishedEvent(sequence, "resync_required", {"patch_schema_version": 2, "reason": reason})


__all__ = ["event_stream_response"]
