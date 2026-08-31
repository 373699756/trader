"""SSE response for the bounded unified V2 event stream."""

from __future__ import annotations

import json
import queue
from collections.abc import Iterator

from flask import Response, stream_with_context

from trader.application.decisions.decision_stream import (
    ResyncEventPayload,
    ResyncReason,
    UnifiedDecisionEventStream,
    UnifiedPublishedEvent,
    UnifiedSubscription,
)
from trader.web.decision_serializers import serialize_event


def decision_event_response(
    stream: UnifiedDecisionEventStream,
    subscription: UnifiedSubscription,
    *,
    heartbeat_seconds: float,
) -> Response:
    def generate() -> Iterator[str]:
        subscriber = subscription.queue
        try:
            yield ": connected\n\n"
            if subscription.resync_reason is not None:
                yield _encode(_resync(subscription.server_sequence_at_open, subscription.resync_reason))
            else:
                for event in subscription.replay:
                    yield _encode(event)
            while True:
                if not stream.is_subscribed(subscriber):
                    yield _encode(_resync(stream.last_sequence(), "slow_subscriber"))
                    return
                try:
                    event = subscriber.get(timeout=max(1.0, heartbeat_seconds))
                except queue.Empty:
                    yield ": heartbeat\n\n"
                    continue
                yield _encode(event)
        finally:
            stream.unsubscribe(subscriber)

    response = Response(stream_with_context(generate()), mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-cache, no-transform"
    response.headers["X-Accel-Buffering"] = "no"
    response.headers["Connection"] = "keep-alive"
    return response


def _encode(event: UnifiedPublishedEvent) -> str:
    payload = json.dumps(
        serialize_event(event),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"id: {event.sequence}\nevent: {event.event_type}\ndata: {payload}\n\n"


def _resync(sequence: int, reason: ResyncReason) -> UnifiedPublishedEvent:
    return UnifiedPublishedEvent(sequence, "resync_required", ResyncEventPayload(reason))


__all__ = ["decision_event_response"]
