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


def test_recommendations_explain_missing_fields(recommendation_policy, application_feature_factory) -> None:
    snapshot = _snapshot(recommendation_policy, application_feature_factory, Strategy.TODAY)
    recommendation = snapshot.recommendations[0]
    values = dict(recommendation.features.values)
    missing_fields = ("news_sentiment", "tail_return_30m", "value_score")
    for field in missing_fields:
        values[field] = None
    features = replace(recommendation.features, values=values, missing_fields=missing_fields)
    snapshot = replace(snapshot, recommendations=(replace(recommendation, features=features),))
    repository = MemoryReadRepository(latest={Strategy.TODAY: snapshot})
    app, _publisher = _app(repository)

    item = app.test_client().get("/api/recommendations/today").get_json()["items"][0]

    assert item["missing_fields"] == list(missing_fields)
    assert item["missing_reasons"] == {
        "news_sentiment": "新闻或公告证据不可用",
        "tail_return_30m": "尾盘分钟数据尚未接入",
        "value_score": "财务或公司事件数据尚未接入",
    }
    assert all(item["features"][field] is None for field in missing_fields)


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


def test_current_query_requires_and_prefers_today_freeze_after_cutoff(
    recommendation_policy,
    application_feature_factory,
) -> None:
    draft = _snapshot(recommendation_policy, application_feature_factory, Strategy.TODAY)
    frozen = replace(
        draft,
        snapshot_id="frozen-today",
        frozen=True,
        published_at=NOW.replace(hour=11, minute=20),
    )
    now = NOW.replace(hour=11, minute=30)
    repository = MemoryReadRepository(
        latest={Strategy.TODAY: draft},
        frozen={(Strategy.TODAY, "2026-07-16"): frozen},
    )
    app, _publisher = _app(repository, now=now)

    ready = app.test_client().get("/api/recommendations/today").get_json()

    assert ready["snapshot_id"] == "frozen-today"
    assert ready["frozen"] is True

    missing_app, _publisher = _app(
        MemoryReadRepository(latest={Strategy.TODAY: draft}),
        now=now,
    )
    missing = missing_app.test_client().get("/api/recommendations/today").get_json()
    assert missing["status"] == "not_ready"
    assert missing["items"] == []


def test_frozen_current_queries_keep_tomorrow_and_d25_isolated(
    recommendation_policy,
    application_feature_factory,
) -> None:
    tomorrow = replace(
        _snapshot(recommendation_policy, application_feature_factory, Strategy.TOMORROW),
        snapshot_id="frozen-tomorrow",
        frozen=True,
    )
    d25 = replace(
        _snapshot(recommendation_policy, application_feature_factory, Strategy.D25),
        snapshot_id="frozen-d25",
        frozen=True,
    )
    repository = MemoryReadRepository(
        frozen={
            (Strategy.TOMORROW, "2026-07-16"): tomorrow,
            (Strategy.D25, "2026-07-16"): d25,
        }
    )
    app, _publisher = _app(repository, now=NOW.replace(hour=14, minute=55))
    client = app.test_client()

    tomorrow_payload = client.get("/api/recommendations/tomorrow").get_json()
    d25_payload = client.get("/api/recommendations/d25").get_json()

    assert tomorrow_payload["snapshot_id"] == "frozen-tomorrow"
    assert d25_payload["snapshot_id"] == "frozen-d25"
    assert tomorrow_payload["strategy"] == "tomorrow"
    assert d25_payload["strategy"] == "d25"
    assert "tail_structure" in tomorrow_payload["items"][0]["scores"]["components"]
    assert "not_overheated" in d25_payload["items"][0]["scores"]["components"]


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
    now: datetime = NOW,
):
    publisher = SnapshotPublisher(
        history_size=history_size,
        client_queue_size=2,
        maximum_subscribers=maximum_subscribers,
    )
    queries = RecommendationQueries(repository, repository, now=lambda: now)
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
