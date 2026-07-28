from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from tests.unit.domain.test_tomorrow_fusion import _evaluation, _request, _selection
from trader.application.current_decisions import CurrentDecisionIndex
from trader.application.tomorrow_events import TomorrowDecisionEventStream
from trader.application.tomorrow_views import TomorrowDecisionQueries
from trader.domain.recommendation.tomorrow_freeze import (
    TomorrowDecisionFreeze,
    build_decision_anchors,
)
from trader.domain.recommendation.tomorrow_fusion import build_tomorrow_decision_epoch
from trader.web import create_app
from trader.web.route_services import TomorrowWebServices

SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 7, 28, 14, 45, tzinfo=SHANGHAI)


class _Clock:
    def now(self) -> datetime:
        return NOW


class _Repository:
    def __init__(self, frozen: TomorrowDecisionFreeze | None = None) -> None:
        self.frozen = frozen

    def save_checkpoint(self, checkpoint) -> None:
        raise AssertionError

    def load_checkpoint(self, trade_date):
        raise AssertionError

    def consume_checkpoint(self, checkpoint_version, *, consumed_at) -> None:
        raise AssertionError

    def commit_freeze(self, frozen) -> None:
        raise AssertionError

    def load_frozen(self, trade_date: date) -> TomorrowDecisionFreeze | None:
        return self.frozen if self.frozen is not None and self.frozen.trade_date == trade_date else None


def test_v2_current_returns_compact_view_and_honors_etag() -> None:
    app, _, _ = _app()
    client = app.test_client()

    response = client.get("/api/v2/tomorrow/current")
    cached = client.get("/api/v2/tomorrow/current", headers={"If-None-Match": response.headers["ETag"]})

    assert response.status_code == 200
    assert response.get_json()["schema_version"] == "tomorrow_decision_view_v2"
    assert response.get_json()["status"] == "ready"
    assert len(response.get_json()["items"]) <= 10
    assert cached.status_code == 304
    assert cached.data == b""


def test_v2_history_validates_date_and_reads_only_formal_record() -> None:
    historical = _decision(NOW - timedelta(days=1))
    frozen = TomorrowDecisionFreeze(
        decision=historical,
        frozen_at=datetime(2026, 7, 27, 14, 50, tzinfo=SHANGHAI),
        freeze_kind="scheduled",
        anchors=build_decision_anchors(historical),
    )
    app, _, _ = _app(frozen)
    client = app.test_client()

    ready = client.get("/api/v2/tomorrow/history?date=2026-07-27")
    cached = client.get(
        "/api/v2/tomorrow/history?date=2026-07-27",
        headers={"If-None-Match": ready.headers["ETag"]},
    )
    invalid = client.get("/api/v2/tomorrow/history?date=2026-7-27")
    missing = client.get("/api/v2/tomorrow/history?date=2026-07-26")

    assert ready.get_json()["frozen"] is True
    assert ready.get_json()["trade_date"] == "2026-07-27"
    assert cached.status_code == 304
    assert invalid.status_code == 400
    assert invalid.get_json()["error"]["code"] == "invalid_date"
    assert missing.status_code == 200
    assert missing.get_json()["status"] == "not_ready"


def test_v2_sse_replays_explicit_cursor_and_default_page_is_complete() -> None:
    app, queries, stream = _app()
    stream.publish_decision(queries.current())
    client = app.test_client()

    response = client.get("/api/v2/events", headers={"Last-Event-ID": "0"}, buffered=False)
    iterator = iter(response.response)
    connected = next(iterator).decode()
    event = next(iterator).decode()
    response.close()
    page = client.get("/v2/tomorrow")

    assert connected == ": connected\n\n"
    assert "event: decision" in event
    payload = json.loads(event.split("data: ", 1)[1])
    assert payload["patch_schema_version"] == 2
    assert payload["projection_version"] == queries.current().projection_version
    assert page.status_code == 200
    assert 'id="tomorrowDecisionTable"' in page.get_data(as_text=True)
    assert "/static/tomorrow_v2.css?rev=" in page.get_data(as_text=True)
    assert "/static/tomorrow_v2.js?rev=" in page.get_data(as_text=True)


def test_v2_routes_are_explicitly_not_ready_without_injected_services() -> None:
    client = create_app().test_client()

    current = client.get("/api/v2/tomorrow/current")
    status = client.get("/api/v2/status")
    events = client.get("/api/v2/events")

    assert current.status_code == 503
    assert current.get_json()["error"]["code"] == "tomorrow_v2_not_ready"
    assert status.status_code == 503
    assert events.status_code == 503


def _app(frozen: TomorrowDecisionFreeze | None = None):
    index = CurrentDecisionIndex()
    index.publish(_decision(), expected_current_version=None)
    queries = TomorrowDecisionQueries(index, _Repository(frozen), _Clock())
    stream = TomorrowDecisionEventStream()
    return create_app(tomorrow=TomorrowWebServices(queries, stream)), queries, stream


def _decision(observed_at: datetime = NOW):
    evaluations = tuple(
        replace(
            evaluation,
            features=replace(
                evaluation.features,
                observed_at=observed_at,
                quote=replace(
                    evaluation.features.quote,
                    source_time=observed_at,
                    received_time=observed_at,
                ),
            ),
        )
        for index in range(12)
        for evaluation in (_evaluation(index, local_score=95.0 - index),)
    )
    return build_tomorrow_decision_epoch(
        replace(
            _request(_selection(evaluations)),
            trade_date=observed_at.date(),
            observed_at=observed_at,
            sequence=12,
        )
    )
