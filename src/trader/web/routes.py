"""Read-only Flask routes for the v2 dashboard."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from flask import Flask, Response, jsonify, render_template, request

from trader.application.publisher import SnapshotPublisher, SubscriberLimitError
from trader.application.queries import RecommendationQueries
from trader.domain.models import Strategy
from trader.web.schemas import empty_snapshot_envelope, error_envelope, snapshot_envelope
from trader.web.sse import event_stream_response

StatusProvider = Callable[[], dict[str, object]]


@dataclass(frozen=True)
class WebApiConfig:
    default_top_n: int = 10
    maximum_top_n: int = 18
    default_event_limit: int = 100
    maximum_event_limit: int = 500
    heartbeat_seconds: float = 15.0


@dataclass(frozen=True)
class WebServices:
    status_provider: StatusProvider
    queries: RecommendationQueries | None = None
    publisher: SnapshotPublisher | None = None
    config: WebApiConfig = WebApiConfig()


def register_routes(app: Flask, services: WebServices) -> None:
    @app.get("/")
    def dashboard() -> str:
        return render_template("index.html")

    @app.get("/api/status")
    def status() -> Response:
        return jsonify(services.status_provider())

    @app.get("/api/recommendations/<strategy_name>")
    def recommendations(strategy_name: str) -> Response | tuple[Response, int]:
        strategy = _strategy(strategy_name)
        if strategy is None:
            return _error(400, "invalid_strategy", "strategy must be today, tomorrow, d25 or long")
        top_n = _bounded_integer(request.args.get("top_n"), services.config.default_top_n)
        if top_n is None or top_n > services.config.maximum_top_n:
            return _error(400, "invalid_top_n", f"top_n must be an integer from 0 to {services.config.maximum_top_n}")
        trade_date = request.args.get("date")
        if trade_date is not None and not _valid_date(trade_date):
            return _error(400, "invalid_date", "date must use YYYY-MM-DD")
        queries = services.queries
        if queries is None:
            return jsonify(empty_snapshot_envelope(strategy.value, trade_date))
        if strategy is Strategy.LONG and trade_date is not None and trade_date != queries.today():
            return _error(400, "long_history_unsupported", "long only supports the current trade date")
        lookup = queries.recommendation(strategy, trade_date)
        if lookup.status == "not_found":
            return _error(404, "snapshot_not_found", "recommendation snapshot does not exist")
        if lookup.snapshot is None:
            return jsonify(empty_snapshot_envelope(strategy.value, trade_date))
        snapshot = lookup.snapshot
        if trade_date is None and request.if_none_match.contains(snapshot.snapshot_id):
            response = Response(status=304)
            response.set_etag(snapshot.snapshot_id)
            return response
        response = jsonify(snapshot_envelope(snapshot, top_n=top_n))
        if trade_date is None:
            response.set_etag(snapshot.snapshot_id)
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/api/recommendation-dates")
    def recommendation_dates() -> Response | tuple[Response, int]:
        strategy = _strategy(request.args.get("strategy", ""))
        if strategy is None:
            return _error(400, "invalid_strategy", "strategy must be today, tomorrow or d25")
        if strategy is Strategy.LONG:
            return _error(400, "long_history_unsupported", "long has no recommendation history")
        dates = services.queries.recommendation_dates(strategy) if services.queries is not None else ()
        return jsonify(
            {
                "schema_version": "v2",
                "status": "ready",
                "strategy": strategy.value,
                "items": list(dates),
                "error": None,
            }
        )

    @app.get("/api/events")
    def events() -> Response | tuple[Response, int]:
        cursor = _bounded_integer(request.args.get("cursor"), 0)
        limit = _bounded_integer(request.args.get("limit"), services.config.default_event_limit)
        if cursor is None:
            return _error(400, "invalid_cursor", "cursor must be a non-negative integer")
        if limit is None or limit < 1 or limit > services.config.maximum_event_limit:
            return _error(
                400,
                "invalid_limit",
                f"limit must be an integer from 1 to {services.config.maximum_event_limit}",
            )
        items = services.queries.pipeline_events(cursor=cursor, limit=limit) if services.queries is not None else ()
        sequences = [
            value for item in items if isinstance(value := item.get("sequence"), int) and not isinstance(value, bool)
        ]
        next_cursor = max(sequences, default=cursor)
        return jsonify(
            {
                "schema_version": "v2",
                "status": "ready",
                "cursor": cursor,
                "next_cursor": next_cursor,
                "items": list(items),
                "error": None,
            }
        )

    @app.get("/api/events/stream")
    def event_stream() -> Response | tuple[Response, int]:
        raw_cursor = request.headers.get("Last-Event-ID", request.args.get("cursor"))
        cursor = _bounded_integer(raw_cursor, 0)
        if cursor is None:
            return _error(400, "invalid_cursor", "Last-Event-ID must be a non-negative integer")
        if services.publisher is None:
            return _error(503, "stream_not_ready", "event stream is not ready")
        try:
            return event_stream_response(
                services.publisher,
                after_sequence=cursor,
                heartbeat_seconds=services.config.heartbeat_seconds,
            )
        except SubscriberLimitError:
            return _error(503, "stream_capacity", "event stream connection limit reached")


def _strategy(raw: str) -> Strategy | None:
    try:
        return Strategy(raw)
    except ValueError:
        return None


def _bounded_integer(raw: str | None, default: int) -> int | None:
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 and str(value) == raw.strip() else None


def _valid_date(raw: str) -> bool:
    try:
        parsed = date.fromisoformat(raw)
    except ValueError:
        return False
    return parsed.isoformat() == raw


def _error(status: int, code: str, message: str) -> tuple[Response, int]:
    return jsonify(error_envelope(code, message)), status


__all__ = ["StatusProvider", "WebApiConfig", "WebServices", "register_routes"]
