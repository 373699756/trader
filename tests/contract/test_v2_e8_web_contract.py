from __future__ import annotations

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

from trader.application.decision_core import UnifiedDecisionIndex
from trader.application.decision_events import build_v2_decision_committed
from trader.application.decision_queries import UnifiedDecisionQueries
from trader.application.decision_stream import UnifiedDecisionEventStream
from trader.domain.market.models import MarketQuote
from trader.domain.recommendation.decision_identity import DecisionItem, ScoredDecision
from trader.domain.recommendation.models import RecommendationAction, Strategy
from trader.web import create_app
from trader.web.route_services import UnifiedWebServices

NOW = datetime(2026, 8, 11, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai"))


class _Clock:
    def now(self) -> datetime:
        return NOW


class _Repository:
    def load(self, strategy: Strategy, trade_date: date):
        return None

    def list_dates(self, strategy: Strategy, *, limit: int = 31) -> tuple[date, ...]:
        return (date(2026, 8, 8),) if strategy is Strategy.TODAY else ()


def test_unified_decision_routes_validate_strategy_date_and_etag() -> None:
    app, _, _ = _app()
    client = app.test_client()

    current = client.get("/api/v2/decisions/today/current")
    cached = client.get(
        "/api/v2/decisions/today/current",
        headers={"If-None-Match": current.headers["ETag"]},
    )
    dates = client.get("/api/v2/decisions/today/dates")
    invalid_strategy = client.get("/api/v2/decisions/weekly/current")
    invalid_date = client.get("/api/v2/decisions/today/history?date=2026-8-8")

    assert current.status_code == 200
    assert current.get_json()["schema_version"] == "v2_decision_view_v1"
    assert current.get_json()["strategy"] == "today"
    assert cached.status_code == 304
    assert dates.get_json()["dates"] == ["2026-08-08"]
    assert invalid_strategy.status_code == 400
    assert invalid_strategy.get_json()["error"]["code"] == "invalid_strategy"
    assert invalid_date.status_code == 400
    assert invalid_date.get_json()["error"]["code"] == "invalid_date"


def test_only_unified_v2_product_routes_are_registered() -> None:
    client = _app()[0].test_client()

    assert client.get("/").status_code == 200
    assert client.get("/api/v2/status").status_code == 200
    for removed in (
        "/api/status",
        "/api/recommendations/today",
        "/api/recommendation-dates?strategy=today",
        "/api/events/stream",
        "/v2/tomorrow",
        "/api/v2/tomorrow/current",
    ):
        assert client.get(removed).status_code == 404


def test_unified_sse_replays_cursor_and_status_exposes_stream_health() -> None:
    app, queries, stream = _app()
    stream.publish_committed(build_v2_decision_committed(_decision()))
    client = app.test_client()

    response = client.get("/api/v2/events", headers={"Last-Event-ID": "0"}, buffered=False)
    iterator = iter(response.response)
    assert next(iterator).decode() == ": connected\n\n"
    event = next(iterator).decode()
    response.close()
    status = client.get("/api/v2/status").get_json()

    assert "event: decision" in event
    assert json.loads(event.split("data: ", 1)[1])["strategy"] == "today"
    assert status["events"]["sequence"] == 1
    assert status["strategies"]["today"]["status"] == queries.current(Strategy.TODAY).status


def test_http_reads_do_not_invoke_external_io() -> None:
    app, _, _ = _app()
    client = app.test_client()

    for path in (
        "/",
        "/api/v2/decisions/today/current",
        "/api/v2/decisions/today/dates",
        "/api/v2/status",
    ):
        assert client.get(path).status_code == 200


def _app():
    index = UnifiedDecisionIndex()
    decision = _decision()
    assert index.publish(decision, expected_version=None).accepted
    queries = UnifiedDecisionQueries(index, _Repository(), _Clock())
    stream = UnifiedDecisionEventStream()
    services = UnifiedWebServices(
        queries, stream, lambda: {"status": "running", "deepseek_budget": {"limit": 168, "used": 12, "remaining": 156}}
    )
    return create_app(services=services), queries, stream


def _decision() -> ScoredDecision:
    return ScoredDecision(
        Strategy.TODAY,
        NOW.date(),
        1,
        NOW,
        "local",
        None,
        (("market", "market:1"),),
        "config:1",
        "strategy:1",
        "fusion:1",
        (
            DecisionItem(
                "600000",
                RecommendationAction.EXECUTABLE,
                True,
                1,
                88.0,
                84.0,
                84.0,
                (("local_score", 84.0),),
                (),
                "threshold_met",
            ),
        ),
        (("hard_filter", 10),),
    )


def _market_quote() -> MarketQuote:
    return MarketQuote(
        code="600000",
        name="浦发银行",
        price=10.25,
        previous_close=10.0,
        open_price=10.1,
        high=10.3,
        low=10.0,
        pct_change=2.5,
        change_5m=0.2,
        speed=0.1,
        volume_ratio=1.2,
        turnover_rate=0.8,
        amount=1_000_000_000.0,
        amplitude=3.0,
        market_cap=300_000_000_000.0,
        industry="银行",
        source="test",
        source_time=NOW,
        received_time=NOW,
        data_version="quote:1",
    )
