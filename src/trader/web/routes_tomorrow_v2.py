"""Parallel read-only tomorrow v2 HTTP and SSE routes."""

from __future__ import annotations

from datetime import date
from functools import partial

from flask import Blueprint, Response, jsonify, render_template, request

from trader.application.tomorrow_events import TomorrowSubscriberLimitError
from trader.application.tomorrow_views import TomorrowDecisionView
from trader.web.route_services import WebServices
from trader.web.tomorrow_v2_serializers import (
    serialize_tomorrow_error,
    serialize_tomorrow_status,
    serialize_tomorrow_view,
)
from trader.web.tomorrow_v2_sse import tomorrow_event_response

RouteResponse = Response | tuple[Response, int]


def create_tomorrow_v2_blueprint(services: WebServices) -> Blueprint:
    blueprint = Blueprint("tomorrow_v2", __name__)
    blueprint.add_url_rule("/v2/tomorrow", "tomorrow_page", _tomorrow_page)
    blueprint.add_url_rule(
        "/api/v2/tomorrow/current",
        "tomorrow_current",
        partial(_tomorrow_current, services),
    )
    blueprint.add_url_rule(
        "/api/v2/tomorrow/history",
        "tomorrow_history",
        partial(_tomorrow_history, services),
    )
    blueprint.add_url_rule(
        "/api/v2/status",
        "tomorrow_status",
        partial(_tomorrow_status, services),
    )
    blueprint.add_url_rule(
        "/api/v2/events",
        "tomorrow_events",
        partial(_tomorrow_events, services),
    )
    return blueprint


def _tomorrow_page() -> str:
    return render_template("tomorrow_v2.html")


def _tomorrow_current(services: WebServices) -> RouteResponse:
    if services.tomorrow_queries is None:
        return _not_ready()
    return _view_response(services.tomorrow_queries.current())


def _tomorrow_history(services: WebServices) -> RouteResponse:
    if services.tomorrow_queries is None:
        return _not_ready()
    raw_date = request.args.get("date", "")
    trade_date = _strict_date(raw_date)
    if trade_date is None:
        return (
            jsonify(
                serialize_tomorrow_error(
                    "invalid_date",
                    "date must use YYYY-MM-DD",
                )
            ),
            400,
        )
    return _view_response(services.tomorrow_queries.history(trade_date))


def _tomorrow_status(services: WebServices) -> RouteResponse:
    if services.tomorrow_queries is None or services.tomorrow_events is None:
        return _not_ready()
    payload = serialize_tomorrow_status(
        services.tomorrow_queries.status(),
        services.tomorrow_events.status(),
    )
    if services.tomorrow_cutover_status is not None:
        payload["shadow"] = dict(services.tomorrow_cutover_status())
    return jsonify(payload)


def _tomorrow_events(services: WebServices) -> RouteResponse:
    stream = services.tomorrow_events
    if stream is None:
        return _not_ready()
    raw_cursor = request.headers.get("Last-Event-ID")
    if raw_cursor is None:
        raw_cursor = request.args.get("cursor")
    cursor = _cursor(raw_cursor)
    if raw_cursor is not None and cursor is None:
        return (
            jsonify(
                serialize_tomorrow_error(
                    "invalid_cursor",
                    "event cursor must be a non-negative integer",
                )
            ),
            400,
        )
    try:
        subscription = stream.open_subscription(cursor)
    except TomorrowSubscriberLimitError:
        return (
            jsonify(
                serialize_tomorrow_error(
                    "stream_capacity",
                    "event stream connection limit reached",
                )
            ),
            503,
        )
    return tomorrow_event_response(
        stream,
        subscription,
        heartbeat_seconds=services.config.heartbeat_seconds,
    )


def _not_ready() -> tuple[Response, int]:
    return (
        jsonify(
            serialize_tomorrow_error(
                "tomorrow_v2_not_ready",
                "tomorrow v2 read services are not ready",
            )
        ),
        503,
    )


def _view_response(view: TomorrowDecisionView) -> Response:
    if view.etag is not None and request.if_none_match.contains(view.etag.strip('"')):
        response = Response(status=304)
        response.headers["ETag"] = view.etag
        return response
    response = jsonify(serialize_tomorrow_view(view))
    if view.etag is not None:
        response.headers["ETag"] = view.etag
    response.headers["Cache-Control"] = "no-cache"
    return response


def _strict_date(raw: str) -> date | None:
    if len(raw) != 10:
        return None
    try:
        parsed = date.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == raw else None


def _cursor(raw: str | None) -> int | None:
    if raw is None:
        return None
    if not raw.isdigit():
        return None
    value = int(raw)
    return value if value <= 9_223_372_036_854_775_807 else None


__all__ = ["create_tomorrow_v2_blueprint"]
