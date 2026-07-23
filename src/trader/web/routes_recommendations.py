"""Recommendation and recommendation-date read routes."""

from __future__ import annotations

from flask import Blueprint, Response, jsonify, request

from trader.application.queries import RecommendationQueries, SnapshotLookup
from trader.domain.recommendation.models import Strategy
from trader.web.request_parsing import (
    RecommendationRequest,
    RequestFailure,
    parse_recommendation_request,
    parse_strategy,
)
from trader.web.route_services import WebServices
from trader.web.serializers import (
    SnapshotDeliveryContext,
    empty_snapshot_envelope,
    serialize_error,
    serialize_recommendation_dates,
    snapshot_envelope,
)

RouteResponse = Response | tuple[Response, int]


def create_recommendation_blueprint(services: WebServices) -> Blueprint:
    blueprint = Blueprint("recommendations", __name__)

    @blueprint.get("/api/recommendations/<strategy_name>")
    def recommendations(strategy_name: str) -> RouteResponse:
        parsed = parse_recommendation_request(
            strategy_name,
            top_n=request.args.get("top_n"),
            trade_date=request.args.get("date"),
            view=request.args.get("view", "official"),
            config=services.config,
        )
        if isinstance(parsed, RequestFailure):
            return _failure_response(parsed, strategy_name, request.args.get("date"))
        return _recommendation_response(parsed, services.queries)

    @blueprint.get("/api/recommendation-dates")
    def recommendation_dates() -> RouteResponse:
        strategy = parse_strategy(request.args.get("strategy", ""))
        failure = _date_request_failure(strategy)
        if failure is not None:
            return _failure_response(failure)
        if strategy is None:
            raise AssertionError("validated date request lost strategy")
        dates = services.queries.recommendation_dates(strategy) if services.queries is not None else ()
        return jsonify(serialize_recommendation_dates(strategy, dates))

    return blueprint


def _recommendation_response(parsed: RecommendationRequest, queries: RecommendationQueries | None) -> RouteResponse:
    if queries is None:
        return jsonify(empty_snapshot_envelope(parsed.strategy.value, parsed.trade_date, view=parsed.view))
    if parsed.strategy is Strategy.LONG and parsed.trade_date is not None and parsed.trade_date != queries.today():
        return _failure_response(
            RequestFailure("long_history_unsupported", "long only supports the current trade date"),
            parsed.strategy.value,
            parsed.trade_date,
        )
    lookup = (
        queries.current_recommendation(parsed.strategy)
        if parsed.trade_date is None and parsed.view == "current"
        else queries.recommendation(parsed.strategy, parsed.trade_date, live=parsed.view == "live")
    )
    if lookup.status == "not_found":
        return _failure_response(
            RequestFailure("snapshot_not_found", "recommendation snapshot does not exist", 404),
            parsed.strategy.value,
            parsed.trade_date,
        )
    if lookup.snapshot is None:
        return jsonify(
            empty_snapshot_envelope(
                parsed.strategy.value,
                parsed.trade_date,
                current_trade_date=lookup.current_trade_date,
                view=parsed.view,
            )
        )
    return _snapshot_response(parsed, lookup)


def _snapshot_response(parsed: RecommendationRequest, lookup: SnapshotLookup) -> Response:
    snapshot = lookup.snapshot
    if snapshot is None:
        raise AssertionError("snapshot response requires a snapshot")
    etag = f"{lookup.etag or snapshot.snapshot_id}:{parsed.view}"
    resolved_view = parsed.view
    if resolved_view == "current":
        resolved_view = "official" if snapshot.frozen else "live"
    if parsed.trade_date is None and request.if_none_match.contains(etag):
        response = Response(status=304)
        response.set_etag(etag)
        return response
    response = jsonify(
        snapshot_envelope(
            snapshot,
            top_n=parsed.top_n,
            delivery=SnapshotDeliveryContext(
                overlay=lookup.overlay,
                requested_date=parsed.trade_date,
                current_trade_date=lookup.current_trade_date,
                historical=lookup.historical,
                current_quotes=lookup.current_quotes,
                view=resolved_view,
            ),
        )
    )
    if parsed.trade_date is None:
        response.set_etag(etag)
        response.headers["Cache-Control"] = "no-cache"
    return response


def _failure_response(
    failure: RequestFailure,
    strategy: str | None = None,
    trade_date: str | None = None,
) -> tuple[Response, int]:
    return (
        jsonify(
            serialize_error(
                failure.code,
                failure.message,
                strategy=strategy,
                trade_date=trade_date,
            )
        ),
        failure.status_code,
    )


def _date_request_failure(strategy: Strategy | None) -> RequestFailure | None:
    if strategy is None:
        return RequestFailure("invalid_strategy", "strategy must be today, tomorrow or d25")
    if strategy is Strategy.LONG:
        return RequestFailure("long_history_unsupported", "long has no recommendation history")
    return None


__all__ = ["create_recommendation_blueprint"]
