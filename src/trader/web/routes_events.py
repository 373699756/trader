"""Event audit and SSE read routes."""

from __future__ import annotations

from flask import Blueprint, Response, jsonify, request

from trader.application.publisher import SubscriberLimitError
from trader.web.request_parsing import RequestFailure, bounded_integer
from trader.web.route_services import WebServices
from trader.web.serializers import serialize_error, serialize_events
from trader.web.sse import event_stream_response

RouteResponse = Response | tuple[Response, int]


def create_event_blueprint(services: WebServices) -> Blueprint:
    blueprint = Blueprint("events", __name__)

    @blueprint.get("/api/events")
    def events() -> RouteResponse:
        cursor = bounded_integer(request.args.get("cursor"), 0)
        limit = bounded_integer(request.args.get("limit"), services.config.default_event_limit)
        failure = _event_request_failure(cursor, limit, services)
        if failure is not None:
            return _failure_response(failure)
        if cursor is None or limit is None:
            raise AssertionError("validated event request lost parsed values")
        items = services.queries.pipeline_events(cursor=cursor, limit=limit) if services.queries is not None else ()
        return jsonify(serialize_events(cursor, list(items)))

    @blueprint.get("/api/events/stream")
    def event_stream() -> RouteResponse:
        raw_cursor = request.headers.get("Last-Event-ID", request.args.get("cursor"))
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


def _event_request_failure(cursor: int | None, limit: int | None, services: WebServices) -> RequestFailure | None:
    if cursor is None:
        return RequestFailure("invalid_cursor", "cursor must be a non-negative integer")
    if limit is None or limit < 1 or limit > services.config.maximum_event_limit:
        return RequestFailure(
            "invalid_limit",
            f"limit must be an integer from 1 to {services.config.maximum_event_limit}",
        )
    return None


def _stream_request_failure(cursor: int | None, publisher_missing: bool) -> RequestFailure | None:
    if cursor is None:
        return RequestFailure("invalid_cursor", "Last-Event-ID must be a non-negative integer")
    if publisher_missing:
        return RequestFailure("stream_not_ready", "event stream is not ready", 503)
    return None


def _failure_response(failure: RequestFailure) -> tuple[Response, int]:
    return jsonify(serialize_error(failure.code, failure.message)), failure.status_code


__all__ = ["create_event_blueprint"]
