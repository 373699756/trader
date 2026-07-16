from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime

from trader.application.publisher import SnapshotPublisher
from trader.application.queries import RecommendationQueries
from trader.application.recommendations import RecommendationEngine
from trader.application.schedule import SHANGHAI
from trader.domain.models import RecommendationSnapshot, Strategy
from trader.web import create_app
from trader.web.routes import WebApiConfig

NOW = datetime(2026, 7, 16, 10, 0, tzinfo=SHANGHAI)


def test_current_recommendations_support_top_zero_and_etag(recommendation_policy, application_feature_factory) -> None:
    snapshot = _snapshot(recommendation_policy, application_feature_factory, Strategy.TODAY)
    repository = MemoryReadRepository(latest={Strategy.TODAY: snapshot})
    app, _publisher = _app(repository)
    client = app.test_client()

    response = client.get("/api/recommendations/today?top_n=0")

    assert response.status_code == 200
    assert response.get_json()["items"] == []
    assert response.get_json()["fusion_mode"] == "local_degraded"
    etag = response.headers["ETag"]
    assert client.get("/api/recommendations/today", headers={"If-None-Match": etag}).status_code == 304


def test_recommendation_validation_and_empty_current(recommendation_policy, application_feature_factory) -> None:
    repository = MemoryReadRepository()
    app, _publisher = _app(repository)
    client = app.test_client()

    empty = client.get("/api/recommendations/tomorrow")

    assert empty.status_code == 200
    assert empty.get_json()["status"] == "not_ready"
    assert client.get("/api/recommendations/unknown").status_code == 400
    assert client.get("/api/recommendations/today?top_n=19").status_code == 400
    assert client.get("/api/recommendations/today?top_n=01").status_code == 400
    assert client.get("/api/recommendations/today?date=2026-02-30").status_code == 400


def test_history_dates_not_found_and_long_rules(recommendation_policy, application_feature_factory) -> None:
    snapshot = replace(
        _snapshot(recommendation_policy, application_feature_factory, Strategy.TOMORROW),
        frozen=True,
    )
    repository = MemoryReadRepository(frozen={(Strategy.TOMORROW, "2026-07-16"): snapshot})
    app, _publisher = _app(repository)
    client = app.test_client()

    historical = client.get("/api/recommendations/tomorrow?date=2026-07-16")

    assert historical.status_code == 200
    assert historical.get_json()["frozen"] is True
    assert client.get("/api/recommendations/tomorrow?date=2026-07-15").status_code == 404
    assert client.get("/api/recommendations/long?date=2026-07-15").status_code == 400
    dates = client.get("/api/recommendation-dates?strategy=tomorrow")
    assert dates.get_json()["items"] == ["2026-07-16"]
    assert client.get("/api/recommendation-dates?strategy=long").status_code == 400


def test_event_query_validates_cursor_and_limit() -> None:
    repository = MemoryReadRepository(events=({"sequence": 3, "status": "success"},))
    app, _publisher = _app(repository)
    client = app.test_client()

    response = client.get("/api/events?cursor=2&limit=1")

    assert response.status_code == 200
    assert response.get_json()["next_cursor"] == 3
    assert client.get("/api/events?cursor=-1").status_code == 400
    assert client.get("/api/events?limit=501").status_code == 400


def test_sse_expired_cursor_and_connection_limit() -> None:
    repository = MemoryReadRepository()
    app, publisher = _app(repository, history_size=2, maximum_subscribers=1)
    publisher.resync("one")
    publisher.resync("two")
    publisher.resync("three")

    response = app.test_client().get("/api/events/stream", headers={"Last-Event-ID": "0"}, buffered=False)
    connected = next(response.response).decode("utf-8")
    first_event = next(response.response).decode("utf-8")
    response.close()

    assert connected == ": connected\n\n"
    assert "event: resync_required" in first_event
    occupied = publisher.open_subscription(publisher.status()["last_sequence"])
    try:
        assert app.test_client().get("/api/events/stream").status_code == 503
    finally:
        publisher.unsubscribe(occupied.queue)


def _app(
    repository: MemoryReadRepository,
    *,
    history_size: int = 8,
    maximum_subscribers: int = 4,
):
    publisher = SnapshotPublisher(
        history_size=history_size,
        client_queue_size=2,
        maximum_subscribers=maximum_subscribers,
    )
    queries = RecommendationQueries(repository, repository, now=lambda: NOW)
    app = create_app(
        lambda: {"schema_version": "v2", "status": "running", "runtime_started": True},
        queries=queries,
        publisher=publisher,
        api_config=WebApiConfig(heartbeat_seconds=1),
    )
    return app, publisher


def _snapshot(recommendation_policy, application_feature_factory, strategy: Strategy) -> RecommendationSnapshot:
    features = tuple(application_feature_factory(f"60000{index}", NOW) for index in range(1, 4))
    return RecommendationEngine(recommendation_policy).build_snapshot(
        strategy,
        features,
        now=NOW,
        phase="today_main" if strategy is Strategy.TODAY else "afternoon",
        trade_date="2026-07-16",
        data_version="fixture-v1",
        review_port=None,
        review_deadline=NOW.replace(hour=11, minute=20),
        max_age_seconds=30,
        filtered_count=2,
        filter_reasons={"stale_quote": 2},
    )


class MemoryReadRepository:
    def __init__(
        self,
        *,
        latest: Mapping[Strategy, RecommendationSnapshot] | None = None,
        frozen: Mapping[tuple[Strategy, str], RecommendationSnapshot] | None = None,
        events: Sequence[Mapping[str, object]] = (),
    ) -> None:
        self._latest = dict(latest or {})
        self._frozen = dict(frozen or {})
        self._events = tuple(events)

    def latest(self, strategy: Strategy) -> RecommendationSnapshot | None:
        return self._latest.get(strategy)

    def load_frozen(self, strategy: Strategy, trade_date: str) -> RecommendationSnapshot | None:
        return self._frozen.get((strategy, trade_date))

    def recommendation_dates(self, strategy: Strategy) -> Sequence[str]:
        return tuple(day for candidate, day in self._frozen if candidate is strategy)

    def list_events(self, *, cursor: int, limit: int) -> Sequence[Mapping[str, object]]:
        return tuple(item for item in self._events if int(item["sequence"]) > cursor)[:limit]

    def initialize(self) -> None:
        return None

    def publish(self, snapshot: RecommendationSnapshot) -> None:
        self._latest[snapshot.strategy] = snapshot

    def freeze(self, snapshot: RecommendationSnapshot) -> None:
        self._frozen[(snapshot.strategy, snapshot.trade_date)] = snapshot

    def recover(self) -> Mapping[str, int]:
        return {}

    def append_event(self, event: Mapping[str, object]) -> None:
        return None
