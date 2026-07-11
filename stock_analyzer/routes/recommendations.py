from __future__ import annotations

from flask import Blueprint, Response, render_template, request

from .. import config
from ..services.app_services import normalize_market
from .common import int_arg, json_result, services


bp = Blueprint("recommendations", __name__)


@bp.route("/")
def index():
    return render_template("index.html", **services().index_context())


@bp.route("/api/recommendations")
def recommendations():
    top_n = int_arg("top_n", config.DEFAULT_TOP_N, minimum=0, maximum=config.RECOMMENDATION_MAX_TOP_N)
    market = normalize_market(request.args.get("market", "all"))
    return json_result(services().recommendations_payload(top_n, market))


@bp.route("/api/recommendations/latest")
def latest_recommendations():
    top_n = int_arg("top_n", config.DEFAULT_TOP_N, minimum=0, maximum=config.RECOMMENDATION_MAX_TOP_N)
    market = normalize_market(request.args.get("market", "all"))
    max_age = int_arg(
        "max_age",
        getattr(config, "RECOMMENDATION_SNAPSHOT_MAX_AGE_SECONDS", 300),
        minimum=0,
        maximum=86400,
    )
    return json_result(services().latest_recommendations_payload(top_n, market, max_age))


@bp.route("/api/recommendations/stream")
def recommendations_stream():
    headers, status = services().empty_stream_status()
    return Response(status=status, headers=headers)


@bp.route("/api/health")
def health():
    return json_result(services().health_payload())


@bp.route("/api/tomorrow-picks")
def tomorrow_picks():
    top_n = int_arg("top_n", config.TOMORROW_TOP_N, minimum=0, maximum=config.RECOMMENDATION_MAX_TOP_N)
    market = normalize_market(request.args.get("market", "all"))
    return json_result(services().horizon_payload("tomorrow_picks", top_n, market))


@bp.route("/api/swing-picks")
def swing_picks():
    top_n = int_arg("top_n", config.DEFAULT_TOP_N, minimum=0, maximum=config.RECOMMENDATION_MAX_TOP_N)
    market = normalize_market(request.args.get("market", "all"))
    return json_result(services().horizon_payload("swing_picks", top_n, market))
