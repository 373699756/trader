from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from threading import Lock
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, request

from tests.unit.domain.test_tomorrow_fusion import _evaluation, _request, _selection
from trader.application.current_decisions import CurrentDecisionIndex
from trader.application.tomorrow_events import TomorrowDecisionEventStream
from trader.application.tomorrow_views import (
    TomorrowDecisionQueries,
    TomorrowLiveQuote,
    TomorrowQuoteOverlay,
    TomorrowQuoteOverlayIndex,
)
from trader.domain.recommendation.tomorrow_fusion import build_tomorrow_decision_epoch
from trader.web import create_app
from trader.web.route_services import TomorrowWebServices

SHANGHAI = ZoneInfo("Asia/Shanghai")


class _Clock:
    def now(self) -> datetime:
        return datetime.now(SHANGHAI)


class _Repository:
    def save_checkpoint(self, checkpoint) -> None:
        raise AssertionError

    def load_checkpoint(self, trade_date):
        raise AssertionError

    def consume_checkpoint(self, checkpoint_version, *, consumed_at) -> None:
        raise AssertionError

    def commit_freeze(self, frozen) -> None:
        raise AssertionError

    def load_frozen(self, trade_date):
        return None


def build_app() -> Flask:
    observed_at = datetime.now(SHANGHAI) - timedelta(seconds=1)
    evaluations = tuple(
        replace(
            evaluation,
            features=replace(
                evaluation.features,
                observed_at=observed_at,
                quote=replace(
                    evaluation.features.quote,
                    name=f"桌面验收{index + 1}",
                    source="offline_fixture",
                    source_time=observed_at,
                    received_time=observed_at,
                ),
            ),
        )
        for index in range(12)
        for evaluation in (_evaluation(index, local_score=96.0 - index),)
    )
    decision = build_tomorrow_decision_epoch(
        replace(
            _request(_selection(evaluations)),
            observed_at=observed_at,
            trade_date=observed_at.date(),
            sequence=12,
        )
    )
    index = CurrentDecisionIndex()
    assert index.publish(decision, expected_current_version=None).accepted
    quotes = TomorrowQuoteOverlayIndex(index)
    queries = TomorrowDecisionQueries(index, _Repository(), _Clock(), quotes=quotes)
    events = TomorrowDecisionEventStream()
    events.publish_decision(queries.current())
    app = create_app(tomorrow=TomorrowWebServices(queries, events))
    metrics = {"current_gets": 0, "updates": 0}
    lock = Lock()

    @app.before_request
    def count_current_gets() -> None:
        if request.path == "/api/v2/tomorrow/current":
            metrics["current_gets"] += 1

    @app.post("/__tomorrow_v2/overlay")
    def publish_overlay():
        with lock:
            metrics["updates"] += 1
            version = f"browser-quote:{metrics['updates']}"
            selected = min(
                (item for item in decision.entries if item.selected),
                key=lambda item: item.rank,
            )
            observed = datetime.now(SHANGHAI)
            overlay = TomorrowQuoteOverlay(
                decision_version=decision.version,
                version=version,
                observed_at=observed,
                quotes=(
                    TomorrowLiveQuote(
                        selected.code,
                        13.37,
                        6.88,
                        "offline_fixture",
                        observed,
                        version,
                    ),
                ),
            )
            expected = None if metrics["updates"] == 1 else f"browser-quote:{metrics['updates'] - 1}"
            assert quotes.publish(overlay, expected_overlay_version=expected).accepted
            result = events.publish_overlay(overlay)
            assert result.accepted
        return jsonify({"version": version, "sequence": result.event.sequence if result.event else None})

    @app.get("/__tomorrow_v2/metrics")
    def fixture_metrics():
        return jsonify(metrics)

    return app


__all__ = ["build_app"]
