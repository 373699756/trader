"""SSE response construction for the bounded tomorrow v2 event stream."""

from __future__ import annotations

import json
import queue
from collections.abc import Iterator

from flask import Response, stream_with_context

from trader.application.tomorrow_events import (
    ResyncReason,
    TomorrowDecisionEventStream,
    TomorrowPublishedEvent,
    TomorrowResyncEventPayload,
    TomorrowSubscription,
)
from trader.web.tomorrow_v2_serializers import serialize_tomorrow_event


def tomorrow_event_response(
    stream: TomorrowDecisionEventStream,
    subscription: TomorrowSubscription,
    *,
    heartbeat_seconds: float,
) -> Response:
    def generate() -> Iterator[str]:
        subscriber = subscription.queue
        try:
            yield ": connected\n\n"
            if subscription.resync_reason is not None:
                yield _encode(
                    _resync_event(
                        subscription.server_sequence_at_open,
                        subscription.resync_reason,
                    )
                )
            else:
                for event in subscription.replay:
                    yield _encode(event)
            while True:
                if not stream.is_subscribed(subscriber):
                    yield _encode(_resync_event(stream.last_sequence(), "slow_subscriber"))
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


def _encode(event: TomorrowPublishedEvent) -> str:
    payload = json.dumps(
        serialize_tomorrow_event(event),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"id: {event.sequence}\nevent: {event.event_type}\ndata: {payload}\n\n"


def _resync_event(sequence: int, reason: ResyncReason) -> TomorrowPublishedEvent:
    return TomorrowPublishedEvent(
        sequence,
        "resync_required",
        TomorrowResyncEventPayload(reason, None),
    )


__all__ = ["tomorrow_event_response"]
