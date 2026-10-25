from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from tests.contract.test_web_contract import NOW, _app, _decision, _Repository
from trader.http_api.route_services import UnifiedWebServices
from trader.recommendation.application.pipeline.freeze_publish.decision_events import BuildDecisionCommitted
from trader.recommendation.application.pipeline.freeze_publish.draft_index import UnifiedDecisionDraftIndex
from trader.recommendation.application.pipeline.freeze_publish.event_stream import UnifiedDecisionEventStream
from trader.recommendation.application.pipeline.freeze_publish.read_only_queries import UnifiedDecisionQueries
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import UnifiedDecisionIndex
from trader.recommendation.domain.publication.decision_identity import LongProjection, LongProjectionItem
from trader.recommendation.domain.publication.models import RecommendationAction
from trader.recommendation.domain.publication.preview import PreviewContext
from trader.web import CreateApp


@pytest.mark.parametrize("profile", ("v1", "v2", "v3", "v5"))
def test_preview_get_sse_and_refresh_share_context_without_losing_algorithm_identity(profile):
    generated_at = NOW.replace(day=16, hour=17)
    context = PreviewContext("preview-http", NOW.date(), generated_at - timedelta(minutes=1))
    source = _decision()
    if profile == "v1":
        source = replace(
            source,
            input_versions=(
                ("score_scale", "learned_rank_percentile_0_100"),
                ("score_model_id", "learned_rank"),
                ("score_model", "learned_rank:" + "b" * 64),
                ("score_contract_hash", "a" * 64),
                ("rank_population", "learned-rank-population:" + "c" * 64),
                ("execution_mode", "production"),
                ("production_authority", "true"),
            ),
            items=(replace(source.items[0], action=RecommendationAction.OBSERVE),),
        )
    completed_items = tuple(
        replace(item, quote=replace(item.quote, source_time=context.market_as_of)) for item in source.items
    )
    preview = replace(
        source,
        trade_date=generated_at.date(),
        observed_at=generated_at,
        preview_context=context,
        items=completed_items,
    )
    index = UnifiedDecisionIndex()
    assert index.publish(preview, expected_version=None).accepted
    stream = UnifiedDecisionEventStream()
    stream.publish_committed(BuildDecisionCommitted(preview))

    class Clock:
        def now(self) -> datetime:
            return generated_at + timedelta(days=1)

    queries = UnifiedDecisionQueries(index, UnifiedDecisionDraftIndex(), _Repository(), Clock())
    preview_status = {
        "status": "running",
        "run_mode": "preview",
        "preview_run_id": context.run_id,
        "market_trade_date": context.market_trade_date.isoformat(),
        "market_as_of": context.market_as_of.isoformat(),
        "preview_started_at": context.started_at.isoformat(),
        "preview_state": "failed",
        "preview_error": "production_model_contract_conflict",
        "preview_internal_detail": "must not be public",
    }
    services = UnifiedWebServices(queries, stream, lambda: preview_status, profile_id=profile)
    client = CreateApp(services=services).test_client()

    response = client.get("/api/decisions/tomorrow/current")
    current = response.get_json()
    repeated = client.get("/api/decisions/tomorrow/current", headers={"If-None-Match": response.headers["ETag"]})
    events = client.get("/api/events", headers={"Last-Event-ID": "0"}, buffered=False)
    iterator = iter(events.response)
    next(iterator)
    event = json.loads(next(iterator).decode().split("data: ", 1)[1])
    events.close()

    assert response.status_code == 200 and repeated.status_code == 304
    status = client.get("/api/status").get_json()
    for name in (
        "run_mode",
        "preview_run_id",
        "market_trade_date",
        "market_as_of",
        "preview_started_at",
        "preview_state",
        "preview_error",
    ):
        assert status[name] == preview_status[name]
    assert "preview_internal_detail" not in status
    for name, expected in (
        ("run_mode", "preview"),
        ("market_trade_date", NOW.date().isoformat()),
        ("market_as_of", context.market_as_of.isoformat()),
        ("generated_at", generated_at.isoformat()),
        ("frozen", False),
        ("frozen_at", None),
    ):
        assert current[name] == event[name] == expected
    assert current["input_versions"] == event["input_versions"] == dict(preview.input_versions)
    assert stream.status().sequence == 1
    assert index.snapshot(preview.strategy).current is preview
    if profile == "v1":
        assert current["input_versions"]["execution_mode"] == "production"
        assert current["input_versions"]["production_authority"] == "true"


def test_nontrading_long_http_keeps_close_change_when_short_models_are_blocked():
    observed = datetime(2026, 10, 24, 10, tzinfo=NOW.tzinfo)
    close = observed.replace(day=23, hour=15)
    projection = LongProjection(
        observed.date(),
        1,
        observed,
        (("watchlist", "long-fixture"),),
        (
            LongProjectionItem(
                "600001",
                "group",
                "close-quote",
                name="长期样例",
                price=12.3,
                pct_change=-1.2,
                source="tencent",
                source_time=close,
                quote_status="live",
            ),
        ),
    )
    index = UnifiedDecisionIndex()
    assert index.publish(projection, expected_version=None).accepted

    class Clock:
        def now(self):
            return observed

    queries = UnifiedDecisionQueries(index, UnifiedDecisionDraftIndex(), _Repository(), Clock())
    client = CreateApp(
        services=UnifiedWebServices(
            queries,
            UnifiedDecisionEventStream(),
            lambda: {"status": "running", "run_mode": "preview", "preview_error": "production_model_contract_conflict"},
        )
    ).test_client()
    response = client.get("/api/decisions/long/current")
    assert response.status_code == 200
    quote = response.get_json()["items"][0]["quote"]
    assert quote["price"] == 12.3 and quote["pct_change"] == -1.2
    assert quote["source_time"] == close.isoformat()
    assert quote["status"] == "live"


def test_live_http_status_does_not_acquire_preview_fields():
    client = _app()[0].test_client()
    status = client.get("/api/status").get_json()
    assert {
        "run_mode",
        "preview_run_id",
        "market_trade_date",
        "market_as_of",
        "preview_started_at",
        "preview_state",
        "preview_error",
    }.isdisjoint(status)
