from __future__ import annotations

from datetime import date, datetime
from threading import Lock
from zoneinfo import ZoneInfo

from flask import Flask, jsonify

from tests.performance.test_pipeline_d4_web import _decision
from trader.application.decision_core import UnifiedDecisionIndex
from trader.application.decision_events import build_v2_decision_committed
from trader.application.decision_queries import UnifiedDecisionQueries
from trader.application.decision_stream import UnifiedDecisionEventStream
from trader.domain.recommendation.decision_identity import LongProjection, LongProjectionItem
from trader.domain.recommendation.models import Strategy
from trader.web import create_app
from trader.web.route_services import UnifiedWebServices

SHANGHAI = ZoneInfo("Asia/Shanghai")


class _Clock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


class _History:
    def load(self, strategy: Strategy, trade_date: date):
        return None

    def list_dates(self, strategy: Strategy, *, limit: int = 31) -> tuple[date, ...]:
        return ()


def build_app() -> Flask:
    observed_at = datetime.now(SHANGHAI).replace(hour=10, minute=30, second=0, microsecond=0)
    index = UnifiedDecisionIndex()
    events = UnifiedDecisionEventStream(history_size=64, client_queue_size=8)
    today = _decision(1)
    today = type(today)(
        today.strategy,
        observed_at.date(),
        today.sequence,
        observed_at,
        today.stage,
        today.parent_version,
        today.input_versions,
        today.config_version,
        today.strategy_version,
        today.fusion_version,
        today.items,
        today.filter_aggregates,
        today.degraded_reasons,
    )
    long = LongProjection(
        observed_at.date(),
        1,
        observed_at,
        (("quotes", "fixture:1"),),
        tuple(
            LongProjectionItem(
                f"601{position:03d}",
                "semiconductor",
                f"quote:{position}",
                f"长期样例{position}",
                "半导体设备",
                10.0 + position,
                float(position) / 10,
                100_000_000.0,
                2.0,
                20_000_000_000.0,
                "offline_fixture",
                observed_at,
                "live",
            )
            for position in range(1, 7)
        ),
    )
    assert index.publish(today, expected_version=None).accepted
    assert index.publish(long, expected_version=None).accepted
    queries = UnifiedDecisionQueries(index, _History(), _Clock(observed_at))
    metrics = {"current_gets": 0, "updates": 0}
    app = create_app(
        services=UnifiedWebServices(
            queries,
            events,
            lambda: {
                "status": "running",
                "runtime_started": True,
                "deepseek_budget": {"limit": 168, "used": 17, "remaining": 151},
            },
        )
    )
    lock = Lock()

    @app.before_request
    def count_current_gets() -> None:
        from flask import request

        if request.path.endswith("/current"):
            metrics["current_gets"] += 1

    @app.post("/__v2/publish")
    def publish_update():
        with lock:
            metrics["updates"] += 1
            next_decision = _decision(metrics["updates"] + 1)
            next_decision = type(next_decision)(
                next_decision.strategy,
                observed_at.date(),
                next_decision.sequence,
                observed_at,
                next_decision.stage,
                next_decision.parent_version,
                next_decision.input_versions,
                next_decision.config_version,
                next_decision.strategy_version,
                next_decision.fusion_version,
                next_decision.items,
                next_decision.filter_aggregates,
                next_decision.degraded_reasons,
            )
            current = index.snapshot(Strategy.TODAY).current
            assert current is not None
            assert index.publish(next_decision, expected_version=current.version).accepted
            event = events.publish_committed(build_v2_decision_committed(next_decision))
        return jsonify({"sequence": event.sequence, "version": next_decision.version})

    @app.get("/__v2/metrics")
    def fixture_metrics():
        return jsonify(metrics)

    return app


__all__ = ["build_app"]
