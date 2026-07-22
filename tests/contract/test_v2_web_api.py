from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime

from trader.application.publisher import SnapshotPublisher
from trader.application.queries import RecommendationQueries
from trader.application.recommendations import RecommendationEngine
from trader.application.schedule import SHANGHAI
from trader.domain.models import (
    DeepSeekReview,
    FilterAudit,
    LiveOverlay,
    LiveQuote,
    RecommendationSnapshot,
    ReviewOutcome,
    RiskFact,
    Strategy,
)
from trader.web import create_app
from trader.web.routes import WebApiConfig

NOW = datetime(2026, 7, 16, 10, 0, tzinfo=SHANGHAI)

RECOMMENDATION_ENVELOPE_KEYS = {
    "schema_version",
    "status",
    "snapshot_id",
    "strategy",
    "trade_date",
    "requested_date",
    "current_trade_date",
    "historical",
    "view",
    "phase",
    "published_at",
    "strategy_version",
    "fusion_mode",
    "stale",
    "frozen",
    "degraded_reasons",
    "filtered_count",
    "items",
    "error",
}
RECOMMENDATION_ITEM_KEYS = {
    "rank",
    "code",
    "name",
    "industry",
    "price",
    "pct_change",
    "turnover_rate",
    "amount",
    "market_cap",
    "source",
    "source_time",
    "quote_data_version",
    "anchor_price",
    "anchor_daily_return_pct",
    "anchor_to_now_pct",
    "action",
    "action_reason",
    "scores",
    "risks",
    "review",
}
RECOMMENDATION_SCORE_KEYS = {
    "local_score",
    "deepseek_score",
    "deepseek_risk_penalty",
    "final_score",
}


def test_current_recommendations_support_top_zero_and_etag(recommendation_policy, application_feature_factory) -> None:
    snapshot = _snapshot(recommendation_policy, application_feature_factory, Strategy.TODAY)
    repository = MemoryReadRepository(latest={Strategy.TODAY: snapshot})
    app, _publisher = _app(repository)
    client = app.test_client()

    response = client.get("/api/recommendations/today?top_n=0")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["schema_version"] == "v3"
    assert set(payload) == RECOMMENDATION_ENVELOPE_KEYS
    assert payload["items"] == []
    assert payload["fusion_mode"] == "local_degraded"
    assert payload["requested_date"] is None
    assert payload["current_trade_date"] == "2026-07-16"
    assert payload["historical"] is False
    etag = response.headers["ETag"]
    assert client.get("/api/recommendations/today", headers={"If-None-Match": etag}).status_code == 304


def test_recommendations_exclude_internal_missing_features_and_evidence(
    recommendation_policy,
    application_feature_factory,
) -> None:
    snapshot = _snapshot(recommendation_policy, application_feature_factory, Strategy.TODAY)
    recommendation = snapshot.recommendations[0]
    values = dict(recommendation.features.values)
    missing_fields = ("news_sentiment", "tail_return_30m", "value_score")
    for field in missing_fields:
        values[field] = None
    features = replace(
        recommendation.features,
        values=values,
        missing_fields=missing_fields,
    )
    snapshot = replace(snapshot, recommendations=(replace(recommendation, features=features),))
    repository = MemoryReadRepository(latest={Strategy.TODAY: snapshot})
    app, _publisher = _app(repository)

    item = app.test_client().get("/api/recommendations/today").get_json()["items"][0]

    assert set(item) == RECOMMENDATION_ITEM_KEYS
    assert set(item["scores"]) == RECOMMENDATION_SCORE_KEYS
    assert "features" not in item
    assert "missing_fields" not in item
    assert "missing_reasons" not in item
    assert "evidence" not in item
    assert item["anchor_to_now_pct"] is None


def test_recommendation_response_excludes_internal_board_and_merge_fields(
    recommendation_policy,
    application_feature_factory,
) -> None:
    snapshot = _snapshot(recommendation_policy, application_feature_factory, Strategy.TODAY)
    repository = MemoryReadRepository(latest={Strategy.TODAY: snapshot})
    app, _publisher = _app(repository)

    payload = app.test_client().get("/api/recommendations/today").get_json()
    item = payload["items"][0]

    assert set(payload) == RECOMMENDATION_ENVELOPE_KEYS
    assert set(item) == RECOMMENDATION_ITEM_KEYS
    assert "metadata" not in payload
    assert "weights" not in payload
    assert "board" not in item
    assert "normalization" not in item


def test_recommendation_validation_and_empty_current(recommendation_policy, application_feature_factory) -> None:
    repository = MemoryReadRepository()
    app, _publisher = _app(repository)
    client = app.test_client()

    empty = client.get("/api/recommendations/tomorrow")

    assert empty.status_code == 200
    empty_payload = empty.get_json()
    assert empty_payload["status"] == "not_ready"
    assert empty_payload["schema_version"] == "v3"
    assert set(empty_payload) == RECOMMENDATION_ENVELOPE_KEYS
    invalid_strategy = client.get("/api/recommendations/unknown")
    assert invalid_strategy.status_code == 400
    assert invalid_strategy.get_json()["schema_version"] == "v3"
    assert set(invalid_strategy.get_json()) == RECOMMENDATION_ENVELOPE_KEYS
    assert client.get("/api/recommendations/today?top_n=19").status_code == 400
    assert client.get("/api/recommendations/today?top_n=01").status_code == 400
    assert client.get("/api/recommendations/today?date=2026-02-30").status_code == 400


def test_recommendation_response_only_exposes_deepseek_review_outcome_and_error(
    recommendation_policy,
    application_feature_factory,
) -> None:
    snapshot = _snapshot(recommendation_policy, application_feature_factory, Strategy.TODAY)
    first = snapshot.recommendations[0]
    reviewed = replace(
        first,
        review=DeepSeekReview(
            code=first.features.quote.code,
            outcome=ReviewOutcome.APPLIED,
            dimensions={},
            risk_facts=(),
            completed_at=NOW,
            rating="neutral",
            review_stage="primary",
            challenger_status="not_run",
            requested_model="deepseek-v4-flash",
            actual_model="deepseek-v4-pro",
            thinking_mode="reasoning",
            raw_confidence=0.9,
            calibrated_confidence=0.8,
            evidence_manifest_hash="api-manifest",
            calibration_version="v1",
        ),
    )
    snapshot = replace(snapshot, recommendations=(reviewed, *snapshot.recommendations[1:]))
    repository = MemoryReadRepository(latest={Strategy.TODAY: snapshot})
    app, _publisher = _app(repository)

    payload = app.test_client().get("/api/recommendations/today").get_json()
    item = next(item for item in payload["items"] if item["code"] == first.features.quote.code)
    assert item["review"] == {"outcome": "applied", "error": ""}


def test_recommendation_response_compacts_and_deduplicates_risks(
    recommendation_policy,
    application_feature_factory,
) -> None:
    snapshot = _snapshot(recommendation_policy, application_feature_factory, Strategy.TODAY)
    recommendation = snapshot.recommendations[0]
    local = RiskFact(
        "crowding:600001",
        "near_limit_crowding",
        "high",
        5.0,
        "local_rule",
        NOW,
        assessment="接近涨跌幅限制",
    )
    duplicate = replace(local, source="deepseek", assessment="模型重复事实")
    model_only = RiskFact(
        "volatility:600001",
        "high_volatility",
        "medium",
        3.0,
        "deepseek",
        NOW,
        assessment="波动率偏高",
    )
    compacted = replace(
        recommendation,
        local_risk_facts=(local,),
        deepseek_risk_facts=(duplicate, model_only),
    )
    snapshot = replace(snapshot, recommendations=(compacted, *snapshot.recommendations[1:]))
    payload = (
        _app(MemoryReadRepository(latest={Strategy.TODAY: snapshot}))[0]
        .test_client()
        .get("/api/recommendations/today")
        .get_json()
    )

    assert payload["items"][0]["risks"] == [
        {
            "risk_code": "near_limit_crowding",
            "severity": "high",
            "penalty": 5.0,
            "assessment": "接近涨跌幅限制",
        },
        {
            "risk_code": "high_volatility",
            "severity": "medium",
            "penalty": 3.0,
            "assessment": "波动率偏高",
        },
    ]


def test_status_includes_route_health_details() -> None:
    app = create_app(
        lambda: {
            "schema_version": "v2",
            "status": "running",
            "runtime_started": True,
            "dependencies": {
                "market_data": {
                    "route": {
                        "status": "success",
                        "used_vendor": "sina",
                        "degraded": True,
                        "fallback_reason": None,
                        "attempted_count": 2,
                        "success_count": 1,
                        "failure_count": 0,
                        "no_data_count": 0,
                        "skipped_count": 1,
                        "attempted_vendors": (
                            {
                                "name": "eastmoney",
                                "status": "skipped",
                                "severity": "required",
                                "error": "circuit_open",
                                "skipped": True,
                                "duration_ms": 1.2,
                            },
                            {
                                "name": "sina",
                                "status": "success",
                                "severity": "required",
                                "error": "",
                                "skipped": False,
                                "duration_ms": 12.1,
                            },
                        ),
                    }
                }
            },
        },
    )

    payload = app.test_client().get("/api/status").get_json()

    route = payload["dependencies"]["market_data"]["route"]
    assert route["status"] == "success"
    assert route["attempted_count"] == 2
    assert route["skipped_count"] == 1
    assert route["attempted_vendors"][0]["name"] == "eastmoney"
    assert route["attempted_vendors"][0]["status"] == "skipped"


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
    assert set(tomorrow_payload["items"][0]["scores"]) == RECOMMENDATION_SCORE_KEYS
    assert set(d25_payload["items"][0]["scores"]) == RECOMMENDATION_SCORE_KEYS


def test_frozen_current_response_applies_overlay_without_changing_anchor_or_snapshot_id(
    recommendation_policy,
    application_feature_factory,
) -> None:
    frozen = replace(
        _snapshot(recommendation_policy, application_feature_factory, Strategy.TOMORROW),
        snapshot_id="frozen-overlay",
        frozen=True,
    )
    live_at = NOW.replace(hour=14, minute=55)
    overlay = LiveOverlay(
        snapshot_id=frozen.snapshot_id,
        strategy=frozen.strategy,
        trade_date=frozen.trade_date,
        version="overlay-v2",
        observed_at=live_at,
        quotes={
            "600001": LiveQuote(
                code="600001",
                price=15.0,
                pct_change=8.0,
                source="tencent",
                source_time=live_at,
                received_time=live_at,
                data_version="live-v2",
            )
        },
    )
    repository = MemoryReadRepository(
        frozen={(Strategy.TOMORROW, "2026-07-16"): frozen},
        overlays={(Strategy.TOMORROW, "2026-07-16"): overlay},
    )
    client = _app(repository, now=live_at)[0].test_client()

    response = client.get("/api/recommendations/tomorrow")
    payload = response.get_json()

    assert payload["snapshot_id"] == "frozen-overlay"
    assert "live_overlay" not in payload
    assert payload["items"][0]["price"] == 15.0
    assert payload["items"][0]["anchor_price"] != 15.0
    assert response.headers["ETag"] != '"frozen-overlay"'
    cached = client.get("/api/recommendations/tomorrow", headers={"If-None-Match": response.headers["ETag"]})
    assert cached.status_code == 304
    assert cached.headers["ETag"] == response.headers["ETag"]


def test_live_draft_response_uses_matching_topk_overlay_and_overlay_etag(
    recommendation_policy,
    application_feature_factory,
) -> None:
    draft = _snapshot(recommendation_policy, application_feature_factory, Strategy.TODAY)
    overlay = LiveOverlay(
        snapshot_id=draft.snapshot_id,
        strategy=draft.strategy,
        trade_date=draft.trade_date,
        version="draft-overlay-v2",
        observed_at=NOW,
        quotes={
            "600001": LiveQuote(
                code="600001",
                price=13.0,
                pct_change=4.0,
                source="tencent",
                source_time=NOW,
                received_time=NOW,
                data_version="draft-live-v2",
            )
        },
    )
    repository = MemoryReadRepository(
        latest={Strategy.TODAY: draft},
        overlays={(Strategy.TODAY, draft.trade_date): overlay},
    )

    response = _app(repository)[0].test_client().get("/api/recommendations/today")
    payload = response.get_json()

    assert "live_overlay" not in payload
    assert payload["items"][0]["price"] == 13.0
    assert response.headers["ETag"] != f'"{draft.snapshot_id}"'


def test_previous_trade_date_is_not_reused_for_current_recommendations(
    recommendation_policy,
    application_feature_factory,
) -> None:
    previous = replace(
        _snapshot(recommendation_policy, application_feature_factory, Strategy.TODAY),
        snapshot_id="previous-freeze",
        trade_date="2026-07-15",
        frozen=True,
    )
    repository = MemoryReadRepository(latest={Strategy.TODAY: previous})
    payload = _app(repository, now=NOW)[0].test_client().get("/api/recommendations/today").get_json()

    assert payload["status"] == "not_ready"
    assert payload["snapshot_id"] is None
    assert payload["trade_date"] is None
    assert payload["items"] == []
    assert payload["stale"] is True
    assert "fallback_date" not in payload
    assert "fallback_reason" not in payload
    assert payload["current_trade_date"] == "2026-07-16"
    assert payload["historical"] is False
    assert payload["degraded_reasons"] == ["snapshot_not_ready"]


def test_explicit_live_view_keeps_same_day_draft_visible_after_freeze_cutoff(
    recommendation_policy,
    application_feature_factory,
) -> None:
    after_cutoff = NOW.replace(hour=14, minute=55)
    draft = replace(
        _snapshot(recommendation_policy, application_feature_factory, Strategy.TOMORROW),
        published_at=after_cutoff.replace(minute=41),
        phase="final_review",
        frozen=False,
    )
    repository = MemoryReadRepository(latest={Strategy.TOMORROW: draft})
    client = _app(repository, now=after_cutoff)[0].test_client()

    official = client.get("/api/recommendations/tomorrow")
    live = client.get("/api/recommendations/tomorrow?view=live")

    assert official.get_json()["status"] == "not_ready"
    assert live.status_code == 200
    payload = live.get_json()
    assert payload["status"] == "ready"
    assert payload["view"] == "live"
    assert payload["snapshot_id"] == draft.snapshot_id
    assert payload["trade_date"] == "2026-07-16"
    assert payload["historical"] is False
    assert payload["frozen"] is False


def test_recommendations_reject_unknown_view() -> None:
    client = _app(MemoryReadRepository())[0].test_client()

    response = client.get("/api/recommendations/today?view=draft")

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_view"


def test_explicit_historical_query_returns_previous_trade_date_snapshot(
    recommendation_policy,
    application_feature_factory,
) -> None:
    previous = replace(
        _snapshot(recommendation_policy, application_feature_factory, Strategy.TODAY),
        snapshot_id="previous-freeze",
        trade_date="2026-07-15",
        frozen=True,
    )
    repository = MemoryReadRepository(frozen={(Strategy.TODAY, "2026-07-15"): previous})

    payload = _app(repository, now=NOW)[0].test_client().get("/api/recommendations/today?date=2026-07-15").get_json()

    assert payload["snapshot_id"] == "previous-freeze"
    assert payload["trade_date"] == "2026-07-15"
    assert payload["requested_date"] == "2026-07-15"
    assert payload["historical"] is True
    assert payload["frozen"] is True


def test_previous_trade_date_never_produces_current_etag(
    recommendation_policy,
    application_feature_factory,
) -> None:
    previous = replace(
        _snapshot(recommendation_policy, application_feature_factory, Strategy.TODAY),
        snapshot_id="previous-freeze",
        trade_date="2026-07-15",
        frozen=True,
    )
    repository = MemoryReadRepository(latest={Strategy.TODAY: previous})

    first = _app(repository, now=NOW)[0].test_client().get("/api/recommendations/today")
    second = _app(repository, now=NOW.replace(day=17))[0].test_client().get("/api/recommendations/today")

    assert "ETag" not in first.headers
    assert "ETag" not in second.headers
    assert first.get_json()["status"] == "not_ready"
    assert second.get_json()["status"] == "not_ready"
    assert first.get_json()["current_trade_date"] == "2026-07-16"
    assert second.get_json()["current_trade_date"] == "2026-07-17"


def test_current_query_reads_snapshot_and_overlay_from_runtime_index(
    recommendation_policy,
    application_feature_factory,
) -> None:
    snapshot = _snapshot(recommendation_policy, application_feature_factory, Strategy.TODAY)
    quote = snapshot.recommendations[0].features.quote
    overlay = LiveOverlay(
        snapshot_id=snapshot.snapshot_id,
        strategy=Strategy.TODAY,
        trade_date=snapshot.trade_date,
        version="runtime-overlay-v1",
        observed_at=NOW,
        quotes={
            quote.code: LiveQuote(
                code=quote.code,
                price=quote.price + 1.0,
                pct_change=7.5,
                source="runtime",
                source_time=NOW,
                received_time=NOW,
                data_version="runtime-v1",
            )
        },
    )
    persisted = CountingReadRepository()
    runtime = MemoryReadRepository(
        latest={Strategy.TODAY: snapshot},
        overlays={(Strategy.TODAY, snapshot.trade_date): overlay},
    )
    queries = RecommendationQueries(
        persisted,
        persisted,
        now=lambda: NOW,
        current_snapshot_reader=runtime,
    )

    lookup = queries.recommendation(Strategy.TODAY)

    assert lookup.snapshot is not None
    assert lookup.snapshot.snapshot_id == snapshot.snapshot_id
    assert lookup.snapshot.replay_input is None
    assert lookup.overlay == overlay
    assert persisted.latest_calls == 0
    assert persisted.overlay_calls == 0


def test_historical_snapshots_are_preloaded_once_as_compact_delivery_views(
    recommendation_policy,
    application_feature_factory,
) -> None:
    snapshot = replace(
        _snapshot(recommendation_policy, application_feature_factory, Strategy.TOMORROW),
        frozen=True,
        filter_details=(FilterAudit("600001", "stale_quote", "<= 20s", 21.0, "fixture", NOW),),
    )
    repository = CountingReadRepository(
        frozen={(Strategy.TOMORROW, snapshot.trade_date): snapshot},
    )
    queries = RecommendationQueries(repository, repository, now=lambda: NOW)

    queries.initialize()
    first = queries.recommendation(Strategy.TOMORROW, snapshot.trade_date)
    second = queries.recommendation(Strategy.TOMORROW, snapshot.trade_date)

    assert first.snapshot is second.snapshot
    assert first.snapshot is not None
    assert first.snapshot.filter_details == ()
    assert repository.frozen_loads == {(Strategy.TOMORROW, snapshot.trade_date): 1}


def test_missing_history_is_not_cached_across_later_freeze(
    recommendation_policy,
    application_feature_factory,
) -> None:
    snapshot = replace(
        _snapshot(recommendation_policy, application_feature_factory, Strategy.D25),
        frozen=True,
    )
    repository = CountingReadRepository()
    queries = RecommendationQueries(repository, repository, now=lambda: NOW)

    missing = queries.recommendation(Strategy.D25, snapshot.trade_date)
    repository._frozen[(Strategy.D25, snapshot.trade_date)] = snapshot
    ready = queries.recommendation(Strategy.D25, snapshot.trade_date)

    assert missing.snapshot is None
    assert ready.snapshot is not None
    assert ready.snapshot.snapshot_id == snapshot.snapshot_id
    assert repository.frozen_loads[(Strategy.D25, snapshot.trade_date)] == 2


def test_historical_snapshot_has_exact_identity_and_current_quote_overlay(
    recommendation_policy,
    application_feature_factory,
) -> None:
    historical = replace(
        _snapshot(recommendation_policy, application_feature_factory, Strategy.TOMORROW),
        snapshot_id="historical-15",
        trade_date="2026-07-15",
        frozen=True,
    )
    current = replace(
        _snapshot(recommendation_policy, application_feature_factory, Strategy.TOMORROW),
        snapshot_id="current-16",
        frozen=True,
    )
    live_at = NOW.replace(hour=14, minute=55)
    overlay = LiveOverlay(
        snapshot_id=current.snapshot_id,
        strategy=current.strategy,
        trade_date=current.trade_date,
        version="current-overlay",
        observed_at=live_at,
        quotes={
            "600001": LiveQuote(
                code="600001",
                price=15.0,
                pct_change=8.0,
                source="tencent",
                source_time=live_at,
                received_time=live_at,
                data_version="live-current",
            )
        },
    )
    repository = MemoryReadRepository(
        latest={Strategy.TOMORROW: current},
        frozen={(Strategy.TOMORROW, "2026-07-15"): historical},
        overlays={(Strategy.TOMORROW, "2026-07-16"): overlay},
    )

    response = _app(repository, now=live_at)[0].test_client().get("/api/recommendations/tomorrow?date=2026-07-15")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["trade_date"] == "2026-07-15"
    assert payload["requested_date"] == "2026-07-15"
    assert payload["current_trade_date"] == "2026-07-16"
    assert payload["historical"] is True
    assert "live_overlay" not in payload
    assert payload["items"][0]["price"] == 15.0
    assert payload["items"][0]["pct_change"] == 8.0
    assert payload["items"][0]["anchor_to_now_pct"] == 25.0


def test_historical_snapshot_uses_current_snapshot_quote_without_overlay(
    recommendation_policy,
    application_feature_factory,
) -> None:
    historical = replace(
        _snapshot(recommendation_policy, application_feature_factory, Strategy.TOMORROW),
        snapshot_id="historical-base-15",
        trade_date="2026-07-15",
        frozen=True,
    )
    historical_item = historical.recommendations[0]
    historical_features = replace(
        historical_item.features,
        quote=replace(historical_item.features.quote, price=10.0, pct_change=1.0),
    )
    historical = replace(
        historical,
        recommendations=(replace(historical_item, features=historical_features), *historical.recommendations[1:]),
    )
    current = replace(
        _snapshot(recommendation_policy, application_feature_factory, Strategy.TOMORROW),
        snapshot_id="current-base-16",
    )
    repository = MemoryReadRepository(
        latest={Strategy.TOMORROW: current},
        frozen={(Strategy.TOMORROW, "2026-07-15"): historical},
    )

    payload = _app(repository, now=NOW)[0].test_client().get("/api/recommendations/tomorrow?date=2026-07-15").get_json()

    assert payload["items"][0]["price"] == 12.0
    assert payload["items"][0]["pct_change"] == 3.0
    assert payload["items"][0]["anchor_to_now_pct"] == 20.0


def test_historical_snapshot_uses_current_quote_index_when_stock_is_not_recommended_today(
    recommendation_policy,
    application_feature_factory,
) -> None:
    historical = replace(
        _snapshot(recommendation_policy, application_feature_factory, Strategy.TOMORROW),
        snapshot_id="historical-index-15",
        trade_date="2026-07-15",
        frozen=True,
    )
    live_at = NOW.replace(hour=10, minute=1)
    current_quote = LiveQuote(
        code="600001",
        price=15.0,
        pct_change=8.0,
        source="tencent",
        source_time=live_at,
        received_time=live_at,
        data_version="p2-current-v1",
    )
    repository = MemoryReadRepository(frozen={(Strategy.TOMORROW, "2026-07-15"): historical})

    payload = (
        _app(
            repository,
            now=live_at,
            current_quotes={"600001": current_quote},
        )[0]
        .test_client()
        .get("/api/recommendations/tomorrow?date=2026-07-15")
        .get_json()
    )

    assert payload["items"][0]["price"] == 15.0
    assert payload["items"][0]["pct_change"] == 8.0
    assert payload["items"][0]["anchor_to_now_pct"] == 25.0


def test_historical_snapshot_does_not_present_anchor_change_as_today_change_when_current_quote_is_missing(
    recommendation_policy,
    application_feature_factory,
) -> None:
    historical = replace(
        _snapshot(recommendation_policy, application_feature_factory, Strategy.TODAY),
        snapshot_id="historical-missing-current-15",
        trade_date="2026-07-15",
        frozen=True,
    )
    repository = MemoryReadRepository(frozen={(Strategy.TODAY, "2026-07-15"): historical})

    payload = (
        _app(
            repository,
            current_quotes={},
        )[0]
        .test_client()
        .get("/api/recommendations/today?date=2026-07-15")
        .get_json()
    )
    item = payload["items"][0]

    assert item["anchor_daily_return_pct"] == 3.0
    assert item["price"] is None
    assert item["pct_change"] is None
    assert item["anchor_to_now_pct"] is None


def test_historical_snapshot_does_not_reuse_previous_day_overlay_as_current_quote(
    recommendation_policy,
    application_feature_factory,
) -> None:
    previous = replace(
        _snapshot(recommendation_policy, application_feature_factory, Strategy.TOMORROW),
        snapshot_id="previous-15",
        trade_date="2026-07-15",
        frozen=True,
    )
    overlay_time = NOW.replace(day=15)
    overlay = LiveOverlay(
        snapshot_id=previous.snapshot_id,
        strategy=previous.strategy,
        trade_date=previous.trade_date,
        version="previous-overlay",
        observed_at=overlay_time,
        quotes={
            "600001": LiveQuote(
                code="600001",
                price=15.0,
                pct_change=8.0,
                source="tencent",
                source_time=overlay_time,
                received_time=overlay_time,
                data_version="previous-live",
            )
        },
    )
    repository = MemoryReadRepository(
        latest={Strategy.TOMORROW: previous},
        frozen={(Strategy.TOMORROW, "2026-07-15"): previous},
        overlays={(Strategy.TOMORROW, "2026-07-15"): overlay},
    )

    payload = _app(repository, now=NOW)[0].test_client().get("/api/recommendations/tomorrow?date=2026-07-15").get_json()

    assert payload["items"][0]["anchor_price"] == previous.recommendations[0].features.quote.price
    assert payload["items"][0]["price"] is None
    assert payload["items"][0]["pct_change"] is None
    assert payload["items"][0]["anchor_to_now_pct"] is None


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
    historical_payload = historical.get_json()
    assert historical_payload["frozen"] is True
    assert historical_payload["requested_date"] == "2026-07-16"
    assert historical_payload["historical"] is True
    missing = client.get("/api/recommendations/tomorrow?date=2026-07-15")
    assert missing.status_code == 404
    assert missing.get_json()["strategy"] == "tomorrow"
    assert missing.get_json()["trade_date"] == "2026-07-15"
    assert client.get("/api/recommendations/long?date=2026-07-15").status_code == 400
    dates = client.get("/api/recommendation-dates?strategy=tomorrow")
    assert dates.get_json()["schema_version"] == "v3"
    assert dates.get_json()["items"] == ["2026-07-16"]
    assert client.get("/api/recommendation-dates?strategy=long").status_code == 400


def test_validation_errors_keep_request_context() -> None:
    app, _publisher = _app(MemoryReadRepository())
    client = app.test_client()

    invalid_top = client.get("/api/recommendations/today?top_n=99&date=2026-07-16")
    invalid_date = client.get("/api/recommendations/d25?date=not-a-date")
    invalid_strategy = client.get("/api/recommendations/not-a-strategy?date=2026-07-16")

    assert invalid_top.status_code == 400
    assert invalid_top.get_json()["strategy"] == "today"
    assert invalid_top.get_json()["trade_date"] == "2026-07-16"
    assert invalid_date.status_code == 400
    assert invalid_date.get_json()["strategy"] == "d25"
    assert invalid_date.get_json()["trade_date"] == "not-a-date"
    assert invalid_strategy.status_code == 400
    assert invalid_strategy.get_json()["strategy"] == "not-a-strategy"
    assert invalid_strategy.get_json()["trade_date"] == "2026-07-16"


def test_event_query_validates_cursor_and_limit() -> None:
    repository = MemoryReadRepository(events=({"sequence": 3, "status": "success"},))
    app, _publisher = _app(repository)
    client = app.test_client()

    response = client.get("/api/events?cursor=2&limit=1")

    assert response.status_code == 200
    assert response.get_json()["schema_version"] == "v3"
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
    current_quotes: Mapping[str, LiveQuote] | None = None,
):
    publisher = SnapshotPublisher(
        history_size=history_size,
        client_queue_size=2,
        maximum_subscribers=maximum_subscribers,
    )
    quote_reader = MemoryCurrentQuoteReader(current_quotes) if current_quotes is not None else None
    queries = RecommendationQueries(repository, repository, now=lambda: now, current_quote_reader=quote_reader)
    app = create_app(
        lambda: {"schema_version": "v2", "status": "running", "runtime_started": True},
        queries=queries,
        publisher=publisher,
        api_config=WebApiConfig(heartbeat_seconds=1),
    )
    return app, publisher


class MemoryCurrentQuoteReader:
    def __init__(self, quotes: Mapping[str, LiveQuote]) -> None:
        self._quotes = dict(quotes)

    def current_quotes(self, codes: Sequence[str]) -> Mapping[str, LiveQuote]:
        return {code: self._quotes[code] for code in codes if code in self._quotes}


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
        overlays: Mapping[tuple[Strategy, str], LiveOverlay] | None = None,
    ) -> None:
        self._latest = dict(latest or {})
        self._frozen = dict(frozen or {})
        self._events = tuple(events)
        self._overlays = dict(overlays or {})

    def latest(self, strategy: Strategy) -> RecommendationSnapshot | None:
        return self._latest.get(strategy)

    def load_frozen(self, strategy: Strategy, trade_date: str) -> RecommendationSnapshot | None:
        return self._frozen.get((strategy, trade_date))

    def recommendation_dates(self, strategy: Strategy) -> Sequence[str]:
        return tuple(day for candidate, day in self._frozen if candidate is strategy)

    def save_live_overlay(self, overlay: LiveOverlay) -> bool:
        self._overlays[(overlay.strategy, overlay.trade_date)] = overlay
        return True

    def load_live_overlay(self, strategy: Strategy, trade_date: str) -> LiveOverlay | None:
        return self._overlays.get((strategy, trade_date))

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


class CountingReadRepository(MemoryReadRepository):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.latest_calls = 0
        self.overlay_calls = 0
        self.frozen_loads: dict[tuple[Strategy, str], int] = {}

    def latest(self, strategy: Strategy) -> RecommendationSnapshot | None:
        self.latest_calls += 1
        return super().latest(strategy)

    def load_frozen(self, strategy: Strategy, trade_date: str) -> RecommendationSnapshot | None:
        key = (strategy, trade_date)
        self.frozen_loads[key] = self.frozen_loads.get(key, 0) + 1
        return super().load_frozen(strategy, trade_date)

    def load_live_overlay(self, strategy: Strategy, trade_date: str) -> LiveOverlay | None:
        self.overlay_calls += 1
        return super().load_live_overlay(strategy, trade_date)
