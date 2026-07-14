from __future__ import annotations

import json
import time

from flask import Blueprint, Response, render_template, request, stream_with_context

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
    top_n = int_arg("top_n", config.DEFAULT_TOP_N, minimum=0, maximum=config.RECOMMENDATION_MAX_TOP_N)
    market = normalize_market(request.args.get("market", "all"))
    poll_seconds = max(0.5, float(getattr(config, "RECOMMENDATION_STREAM_POLL_SECONDS", 1.0)))
    heartbeat_seconds = max(5.0, float(getattr(config, "RECOMMENDATION_STREAM_HEARTBEAT_SECONDS", 15.0)))

    @stream_with_context
    def events():
        last_event_id = ""
        last_heartbeat = 0.0
        yield "retry: 2000\n\n"
        while True:
            payload, status = services().recommendations_payload(top_n, market)
            now = time.monotonic()
            if status == 200 and isinstance(payload, dict):
                meta = dict(payload.get("meta") or {})
                health = dict(payload.get("health") or {})
                event_id = str(
                    meta.get("quote_version")
                    or meta.get("quote_timestamp")
                    or health.get("last_quote_refresh")
                    or meta.get("generated_at")
                    or ""
                )
                if event_id and event_id != last_event_id:
                    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                    yield f"id: {event_id}\nevent: recommendations\ndata: {body}\n\n"
                    last_event_id = event_id
                    last_heartbeat = now
                elif now - last_heartbeat >= heartbeat_seconds:
                    yield ": keep-alive\n\n"
                    last_heartbeat = now
            elif now - last_heartbeat >= heartbeat_seconds:
                yield ": waiting-for-market-data\n\n"
                last_heartbeat = now
            time.sleep(poll_seconds)

    return Response(
        events(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


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
