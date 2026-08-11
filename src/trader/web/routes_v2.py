"""The complete read-only V2 HTTP product surface."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from functools import partial

from flask import Blueprint, Response, jsonify, render_template, request

from trader.application.decision_queries import DecisionView
from trader.application.decision_stream import UnifiedSubscriberLimitError
from trader.domain.recommendation.models import Strategy
from trader.web.decision_serializers import serialize_decision_view, serialize_error
from trader.web.decision_sse import decision_event_response
from trader.web.route_services import UnifiedWebServices

RouteResponse = Response | tuple[Response, int]


def create_v2_blueprint(services: UnifiedWebServices | None) -> Blueprint:
    blueprint = Blueprint("v2_product", __name__)
    blueprint.add_url_rule("/", "root", _root)
    blueprint.add_url_rule(
        "/api/v2/decisions/<strategy_name>/current",
        "decision_current",
        partial(_current, services),
    )
    blueprint.add_url_rule(
        "/api/v2/decisions/<strategy_name>/history",
        "decision_history",
        partial(_history, services),
    )
    blueprint.add_url_rule(
        "/api/v2/decisions/<strategy_name>/dates",
        "decision_dates",
        partial(_dates, services),
    )
    blueprint.add_url_rule("/api/v2/status", "status", partial(_status, services))
    blueprint.add_url_rule("/api/v2/events", "events", partial(_events, services))
    return blueprint


def _root() -> str:
    return render_template("index.html")


def _current(services: UnifiedWebServices | None, strategy_name: str) -> RouteResponse:
    strategy = _strategy(strategy_name)
    if strategy is None:
        return _error("invalid_strategy", "strategy must be today, tomorrow, d25 or long", 400)
    if services is None:
        return _not_ready()
    return _view_response(services.queries.current(strategy))


def _history(services: UnifiedWebServices | None, strategy_name: str) -> RouteResponse:
    strategy = _strategy(strategy_name)
    if strategy is None:
        return _error("invalid_strategy", "strategy must be today, tomorrow, d25 or long", 400)
    raw_date = request.args.get("date", "")
    trade_date = _strict_date(raw_date)
    if trade_date is None:
        return _error("invalid_date", "date must use YYYY-MM-DD", 400)
    if services is None:
        return _not_ready()
    return _view_response(services.queries.history(strategy, trade_date))


def _dates(services: UnifiedWebServices | None, strategy_name: str) -> RouteResponse:
    strategy = _strategy(strategy_name)
    if strategy is None:
        return _error("invalid_strategy", "strategy must be today, tomorrow, d25 or long", 400)
    if services is None:
        return _not_ready()
    return jsonify(
        {
            "schema_version": "v2_decision_dates_v1",
            "strategy": strategy.value,
            "dates": [value.isoformat() for value in services.queries.dates(strategy)],
        }
    )


def _status(services: UnifiedWebServices | None) -> RouteResponse:
    if services is None:
        return _not_ready()
    try:
        runtime = services.status_provider()
    except (OSError, RuntimeError, TypeError, ValueError):
        runtime = {"status": "degraded", "degraded_reasons": ("runtime_status_unavailable",)}
    stream = services.events.status()
    strategies = {strategy.value: _strategy_status(services.queries.current(strategy)) for strategy in Strategy}
    return jsonify(
        {
            "schema_version": "v2_status_v1",
            "status": str(runtime.get("status", "running")),
            "phase": runtime.get("phase"),
            "runtime_started": bool(runtime.get("runtime_started", True)),
            "last_error": runtime.get("last_error"),
            "deepseek_budget": _budget(runtime),
            "degraded_reasons": list(_reasons(runtime)),
            "strategies": strategies,
            "events": {
                "sequence": stream.sequence,
                "history_size": stream.history_size,
                "subscriber_count": stream.subscriber_count,
                "slow_subscriber_drops": stream.slow_subscriber_drops,
            },
        }
    )


def _events(services: UnifiedWebServices | None) -> RouteResponse:
    if services is None:
        return _not_ready()
    raw_cursor = request.headers.get("Last-Event-ID")
    if raw_cursor is None:
        raw_cursor = request.args.get("cursor")
    cursor = _cursor(raw_cursor)
    if raw_cursor is not None and cursor is None:
        return _error("invalid_cursor", "event cursor must be a non-negative integer", 400)
    try:
        subscription = services.events.open_subscription(cursor)
    except UnifiedSubscriberLimitError:
        return _error("stream_capacity", "event stream connection limit reached", 503)
    return decision_event_response(
        services.events,
        subscription,
        heartbeat_seconds=services.config.heartbeat_seconds,
    )


def _view_response(view: DecisionView) -> Response:
    if view.etag is not None and request.if_none_match.contains(view.etag):
        response = Response(status=304)
        response.set_etag(view.etag)
        return response
    response = jsonify(serialize_decision_view(view))
    if view.etag is not None:
        response.set_etag(view.etag)
    response.headers["Cache-Control"] = "no-cache"
    return response


def _strategy_status(view: DecisionView) -> dict[str, object]:
    return {
        "status": view.status,
        "trade_date": view.trade_date.isoformat() if view.trade_date is not None else None,
        "decision_version": view.decision_version,
        "data_age_seconds": view.data_age_seconds,
        "stage": view.stage,
        "score_status": view.score_status,
        "frozen": view.frozen,
        "coverage": {
            "candidate_count": view.coverage.candidate_count,
            "evaluated_count": view.coverage.evaluated_count,
            "rejected_count": view.coverage.rejected_count,
            "selected_count": view.coverage.selected_count,
        },
        "degraded_reasons": list(view.degraded_reasons),
    }


def _budget(runtime: Mapping[str, object]) -> Mapping[str, object]:
    direct = runtime.get("deepseek_budget")
    if isinstance(direct, Mapping):
        return _json_mapping(direct)
    dependencies = runtime.get("dependencies")
    if isinstance(dependencies, Mapping):
        deepseek = dependencies.get("deepseek")
        if isinstance(deepseek, Mapping) and isinstance(deepseek.get("budget"), Mapping):
            budget = deepseek["budget"]
            if isinstance(budget, Mapping):
                return _json_mapping(budget)
    return {"available": False, "error": "budget_status_unavailable"}


def _reasons(runtime: Mapping[str, object]) -> tuple[str, ...]:
    reasons = runtime.get("degraded_reasons", ())
    if isinstance(reasons, (list, tuple)):
        return tuple(str(reason) for reason in reasons)
    return ()


def _json_mapping(value: Mapping[object, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, item in value.items():
        if isinstance(item, Mapping):
            result[str(key)] = _json_mapping(item)
        elif isinstance(item, (list, tuple)):
            result[str(key)] = [_json_mapping(nested) if isinstance(nested, Mapping) else nested for nested in item]
        else:
            result[str(key)] = item
    return result


def _strategy(raw: str) -> Strategy | None:
    try:
        return Strategy(raw)
    except ValueError:
        return None


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


def _not_ready() -> tuple[Response, int]:
    return _error("v2_not_ready", "V2 read services are not ready", 503)


def _error(code: str, message: str, status_code: int) -> tuple[Response, int]:
    return jsonify(serialize_error(code, message)), status_code


__all__ = ["create_v2_blueprint"]
