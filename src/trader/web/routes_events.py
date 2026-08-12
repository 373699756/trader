"""SSE event stream route."""

from __future__ import annotations

import json
import queue
from collections.abc import Iterator

from flask import Blueprint, Response, jsonify, request, stream_with_context

from trader.application.decision_stream import (
    UnifiedDecisionEventStream,
    UnifiedPublishedEvent,
    UnifiedSubscriberLimitError,
)
from trader.application.publisher import SubscriberLimitError
from trader.web.request_parsing import RequestFailure, bounded_integer
from trader.web.route_services import WebServices
from trader.web.serializers import serialize_error
from trader.web.sse import event_stream_response

RouteResponse = Response | tuple[Response, int]


def create_event_blueprint(services: WebServices) -> Blueprint:
    blueprint = Blueprint("events", __name__)

    @blueprint.get("/api/events/stream")
    def event_stream() -> RouteResponse:
        raw_cursor = request.headers.get("Last-Event-ID", request.args.get("cursor"))
        if services.decision_events is not None:
            return _unified_event_stream(
                services.decision_events,
                raw_cursor,
                heartbeat_seconds=services.config.heartbeat_seconds,
            )
        publisher = services.publisher
        if raw_cursor is None and publisher is None:
            return _failure_response(RequestFailure("stream_not_ready", "event stream is not ready", 503))
        cursor = (
            publisher.last_sequence()
            if raw_cursor is None and publisher is not None
            else bounded_integer(raw_cursor, 0)
        )
        failure = _stream_request_failure(cursor, publisher is None)
        if failure is not None:
            return _failure_response(failure)
        if cursor is None or publisher is None:
            raise AssertionError("validated stream request lost publisher or cursor")
        try:
            return event_stream_response(
                publisher,
                after_sequence=cursor,
                heartbeat_seconds=services.config.heartbeat_seconds,
            )
        except SubscriberLimitError:
            return _failure_response(RequestFailure("stream_capacity", "event stream connection limit reached", 503))

    return blueprint


def _unified_event_stream(
    stream: UnifiedDecisionEventStream,
    raw_cursor: str | None,
    *,
    heartbeat_seconds: float,
) -> RouteResponse:
    cursor = bounded_integer(raw_cursor, stream.last_sequence()) if raw_cursor is not None else None
    if raw_cursor is not None and cursor is None:
        return _failure_response(RequestFailure("invalid_cursor", "Last-Event-ID must be a non-negative integer"))
    try:
        subscription = stream.open_subscription(cursor)
    except UnifiedSubscriberLimitError:
        return _failure_response(RequestFailure("stream_capacity", "event stream connection limit reached", 503))

    def generate() -> Iterator[str]:
        subscriber = subscription.queue
        try:
            yield ": connected\n\n"
            if subscription.resync_reason is not None:
                yield _legacy_resync(subscription.server_sequence_at_open, subscription.resync_reason)
            else:
                for event in subscription.replay:
                    yield _legacy_resync(event.sequence, _event_reason(event))
            while True:
                if not stream.is_subscribed(subscriber):
                    yield _legacy_resync(stream.last_sequence(), "slow_subscriber")
                    return
                try:
                    event = subscriber.get(timeout=max(1.0, heartbeat_seconds))
                except queue.Empty:
                    yield ": heartbeat\n\n"
                    continue
                yield _legacy_resync(event.sequence, _event_reason(event))
        finally:
            stream.unsubscribe(subscriber)

    response = Response(stream_with_context(generate()), mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-cache, no-transform"
    response.headers["X-Accel-Buffering"] = "no"
    response.headers["Connection"] = "keep-alive"
    return response


def _event_reason(event: UnifiedPublishedEvent) -> str:
    return "decision_updated" if event.event_type == "decision" else "quote_updated"


def _legacy_resync(sequence: int, reason: str) -> str:
    payload = json.dumps(
        {"patch_schema_version": 2, "reason": reason},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"id: {sequence}\nevent: resync_required\ndata: {payload}\n\n"


def _stream_request_failure(cursor: int | None, publisher_missing: bool) -> RequestFailure | None:
    if cursor is None:
        return RequestFailure("invalid_cursor", "Last-Event-ID must be a non-negative integer")
    if publisher_missing:
        return RequestFailure("stream_not_ready", "event stream is not ready", 503)
    return None


def _failure_response(failure: RequestFailure) -> tuple[Response, int]:
    return jsonify(serialize_error(failure.code, failure.message)), failure.status_code


__all__ = ["create_event_blueprint"]
