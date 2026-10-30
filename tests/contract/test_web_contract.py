from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from trader.http_api.route_services import ProfiledWebServices, UnifiedWebServices, WebApiConfig
from trader.recommendation.application.pipeline.freeze_publish.draft_index import UnifiedFreezeDraftIndex
from trader.recommendation.application.pipeline.freeze_publish.event_stream import UnifiedFreezeEventStream
from trader.recommendation.application.pipeline.freeze_publish.freeze_events import BuildFreezeCommitted
from trader.recommendation.application.pipeline.freeze_publish.read_only_queries import (
    FreezeQuerySources,
    UnifiedFreezeQueries,
)
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import UnifiedFreezeIndex
from trader.recommendation.domain.evidence.document_reference import CompanyDocumentReference
from trader.recommendation.domain.evidence.pipeline import (
    PIPELINE_STAGE_ORDER,
    PipelineStageStatus,
    RecommendationPipelineStatus,
)
from trader.recommendation.domain.market.models import Board, MarketQuote
from trader.recommendation.domain.publication.freeze_identity import (
    FreezeItem,
    FreezeModelDiagnostics,
    FreezeQuote,
    LongProjection,
    LongProjectionItem,
    ScoredFreeze,
)
from trader.recommendation.domain.publication.models import RecommendationAction, Strategy
from trader.training.domain.model_publication import HeadTrainingStatus, ModelPublicationIdentity
from trader.training.infra.history.history_training_projection import ProjectHeadTrainingStatus
from trader.web import CreateApp

NOW = datetime(2026, 8, 11, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
PIPELINE_STAGE_KEYS = PIPELINE_STAGE_ORDER


class _Clock:
    def now(self) -> datetime:
        return NOW


class _Repository:
    def load(self, strategy: Strategy, trade_date: date):
        return None

    def list_dates(self, strategy: Strategy, *, limit: int = 31) -> tuple[date, ...]:
        return (date(2026, 8, 8),) if strategy is Strategy.TOMORROW else ()


def test_unified_freeze_routes_validate_strategy_date_and_etag() -> None:
    app, _, _ = _app()
    client = app.test_client()

    current = client.get("/api/freezes/tomorrow/current")
    cached = client.get(
        "/api/freezes/tomorrow/current",
        headers={"If-None-Match": current.headers["ETag"]},
    )
    dates = client.get("/api/freezes/tomorrow/dates")
    invalid_strategy = client.get("/api/freezes/weekly/current")
    invalid_date = client.get("/api/freezes/tomorrow/history?date=2026-8-8")

    assert current.status_code == 200
    assert current.get_json()["schema_version"] == "freeze_view"
    assert current.get_json()["draft"] is None
    assert current.get_json()["strategy"] == "tomorrow"
    assert current.get_json()["publication_id"] == current.get_json()["freeze_version"]
    assert current.get_json()["items"][0]["name"] == "浦发银行"
    assert current.get_json()["items"][0]["board"] == "main"
    assert current.get_json()["items"][0]["industry"] == "银行"
    assert current.get_json()["items"][0]["selection_rank"] == 1
    assert current.get_json()["items"][0]["scores"]["predicted_net_excess_pct"] == 1.25
    assert [item["code"] for item in current.get_json()["top_scores"]] == ["600000"]
    assert current.get_json()["input_versions"]["score_scale"] == "weighted_evidence_quality_0_100"
    assert current.get_json()["input_versions"]["score_model"] == ("daily_reconstructible_ensemble:model-hash")
    assert current.get_json()["pipeline"]["current_stage"] == "action_publish"
    assert [stage["key"] for stage in current.get_json()["pipeline"]["stages"]] == list(PIPELINE_STAGE_KEYS)
    assert current.get_json()["items"][0]["quote"] == {
        "price": 10.25,
        "pct_change": 2.5,
        "amount": 1_000_000_000.0,
        "turnover_rate": 0.8,
        "market_cap": 300_000_000_000.0,
        "source": "fixture",
        "source_time": NOW.isoformat(),
        "status": "freeze_anchor",
    }
    assert cached.status_code == 304
    assert dates.get_json()["dates"] == ["2026-08-08"]
    assert invalid_strategy.status_code == 400
    assert invalid_strategy.get_json()["error"]["code"] == "invalid_strategy"
    assert invalid_date.status_code == 400
    assert invalid_date.get_json()["error"]["code"] == "invalid_date"


def test_only_unified_product_routes_are_registered() -> None:
    client = _app()[0].test_client()

    assert client.get("/").status_code == 200
    assert client.get("/api/status").status_code == 200
    for removed in (
        "/api/recommendations/today",
        "/api/recommendation-dates?strategy=today",
        "/api/events/stream",
        "/v2/tomorrow",
        "/api/v2/status",
        "/api/v2/tomorrow/current",
    ):
        assert client.get(removed).status_code == 404


def test_company_evidence_links_are_bounded_read_only_and_explicitly_projected():
    calls = []

    class Links:
        def document_links(self, code, identities):
            calls.append((code, identities))
            return (
                CompanyDocumentReference("notice", "正式公告", "https://example.test/notice.pdf", NOW, "extracted"),
            )

    queries = UnifiedFreezeQueries(
        UnifiedFreezeIndex(),
        UnifiedFreezeDraftIndex(),
        _Repository(),
        _Clock(),
        sources=FreezeQuerySources(company_documents=Links()),
    )
    client = CreateApp(services=UnifiedWebServices(queries, UnifiedFreezeEventStream(), lambda: {})).test_client()
    response = client.get("/api/company/600001/documents?evidence_id=notice&evidence_id=notice")
    assert response.status_code == 200
    assert calls == [("600001", ("notice",))]
    assert response.get_json()["documents"] == [
        {
            "evidence_id": "notice",
            "title": "正式公告",
            "url": "https://example.test/notice.pdf",
            "published_at": NOW.isoformat(),
            "body_checked": True,
        }
    ]
    for query in (
        "/api/company/600001/documents",
        "/api/company/invalid/documents?evidence_id=notice",
        "/api/company/600001/documents?" + "&".join("evidence_id=" + str(i) for i in range(33)),
    ):
        assert client.get(query).status_code == 400
    assert len(calls) == 1


def test_profiled_web_selects_v2_or_v3_while_long_remains_owned_by_v2() -> None:
    _, queries, stream = _app()

    def status_provider() -> dict[str, str]:
        return {"status": "running", "phase": "morning_main"}

    services = ProfiledWebServices(
        {
            "v2": UnifiedWebServices(queries, stream, status_provider, profile_id="v2"),
            "v3": UnifiedWebServices(queries, stream, status_provider, profile_id="v3"),
        },
        default_profile="v3",
        long_profile="v2",
    )
    client = CreateApp(services=services).test_client()

    assert client.get("/api/freezes/tomorrow/current").get_json()["scoring_profile"] == "v3"
    assert client.get("/api/freezes/tomorrow/current?profile=v2").get_json()["scoring_profile"] == "v2"
    assert client.get("/api/freezes/long/current?profile=v3").get_json()["scoring_profile"] == "v2"
    status = client.get("/api/status?profile=v3").get_json()
    assert status["selected_profile"] == "v3"
    assert status["available_profiles"] == ["v2", "v3"]
    assert client.get("/api/status?profile=v4").status_code == 400
    page = client.get("/").get_data(as_text=True)
    assert 'name="trader-default-scoring-profile" content="v3"' in page
    assert 'name="trader-long-scoring-profile" content="v2"' in page


@pytest.mark.parametrize("production_authority", (True, False), ids=("production", "legacy_shadow"))
def test_v1_lane_keeps_rank_scale_and_execution_identity_in_get_and_sse(production_authority: bool) -> None:
    _, existing_queries, existing_stream = _app()
    source = _freeze()
    freeze = replace(
        source,
        input_versions=(
            ("score_scale", "learned_rank_percentile_0_100"),
            ("score_model_id", "learned_rank"),
            ("score_model", "learned_rank:" + "b" * 64),
            ("score_contract_hash", "a" * 64),
            ("rank_population", "learned-rank-population:" + "c" * 64),
            ("execution_mode", "production" if production_authority else "shadow"),
            ("production_authority", "true" if production_authority else "false"),
        ),
        items=(
            replace(
                source.items[0],
                action=RecommendationAction.EXECUTABLE if production_authority else RecommendationAction.OBSERVE,
                final_score=99.5,
            ),
        ),
    )
    index = UnifiedFreezeIndex()
    assert index.publish(freeze, expected_version=None).accepted
    queries = UnifiedFreezeQueries(index, UnifiedFreezeDraftIndex(), _Repository(), _Clock())
    stream = UnifiedFreezeEventStream()
    stream.publish_committed(BuildFreezeCommitted(freeze))
    status = {
        "scoring_profile": {
            "profile_id": "v1",
            "heads": {
                "tomorrow": {
                    "score_scale": "learned_rank_percentile_0_100",
                    "production_authority": production_authority,
                    "private_internal": "must-not-leak",
                }
            },
        }
    }
    services = ProfiledWebServices(
        {
            "v1": UnifiedWebServices(queries, stream, lambda: status, profile_id="v1"),
            "v2": UnifiedWebServices(existing_queries, existing_stream, lambda: {}, profile_id="v2"),
            "v3": UnifiedWebServices(existing_queries, existing_stream, lambda: {}, profile_id="v3"),
        },
        default_profile="v2",
        long_profile="v2",
    )
    client = CreateApp(services=services).test_client()

    response = client.get("/api/freezes/tomorrow/current?profile=v1")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["scoring_profile"] == "v1"
    assert payload["input_versions"] == dict(freeze.input_versions)
    assert payload["items"][0]["scores"]["local"] == 84.0
    assert payload["items"][0]["scores"]["final"] == 99.5
    assert payload["items"][0]["action"] == ("executable" if production_authority else "observe")
    assert client.get("/api/freezes/tomorrow/current?profile=v2").get_json()["items"][0]["scores"]["final"] == 84.0
    long = client.get("/api/freezes/long/current?profile=v1").get_json()
    assert long["scoring_profile"] == "v2"
    assert long["score_status"] == "not_applicable"
    assert "scores" not in long["items"][0]
    status_response = client.get("/api/status?profile=v1")
    head = status_response.get_json()["scoring_profile"]["heads"]["tomorrow"]
    assert head == {"score_scale": "learned_rank_percentile_0_100", "production_authority": production_authority}
    assert "must-not-leak" not in status_response.get_data(as_text=True)
    page = client.get("/").get_data(as_text=True)
    assert 'data-profile="v1"' in page
    assert 'aria-label="V1"' in page
    assert "V1 · Shadow" not in page

    events = client.get("/api/events?profile=v1", headers={"Last-Event-ID": "0"}, buffered=False)
    iterator = iter(events.response)
    next(iterator)
    event = json.loads(next(iterator).decode().split("data: ", 1)[1])
    events.close()
    assert event["input_versions"] == payload["input_versions"]
    assert event["upserts"][0]["scores"]["local_score"] == 84.0
    assert event["upserts"][0]["scores"]["final_score"] == 99.5


def test_long_http_projection_is_current_only_and_has_no_scoring_or_freeze_fields() -> None:
    client = _app()[0].test_client()

    current = client.get("/api/freezes/long/current")
    dates = client.get("/api/freezes/long/dates")
    history = client.get("/api/freezes/long/history?date=2026-08-11")

    assert current.status_code == 200
    payload = current.get_json()
    assert payload["strategy"] == "long"
    assert payload["view"] == "current"
    assert payload["score_status"] == "not_applicable"
    assert payload["publication_id"]
    assert payload["coverage"] == {"item_count": 1, "available_quote_count": 1}
    assert set(payload).isdisjoint({"frozen", "frozen_at", "freeze_kind", "top_scores", "selection_diagnostics"})
    assert set(payload["items"][0]).isdisjoint({"rank", "selection_rank", "scores", "score_status"})
    assert dates.get_json() == {
        "schema_version": "freeze_dates",
        "strategy": "long",
        "scoring_profile": "v2",
        "dates": [],
    }
    assert history.get_json()["status"] == "not_applicable"
    assert history.get_json()["items"] == []


def test_root_injects_configured_web_snapshot_retention_margin() -> None:
    app, queries, stream = _app()
    services = UnifiedWebServices(
        queries,
        stream,
        lambda: {"status": "running", "phase": "morning_main"},
        WebApiConfig(heartbeat_seconds=15, snapshot_retention_seconds=35),
    )

    page = CreateApp(services=services).test_client().get("/").get_data(as_text=True)

    assert 'name="trader-web-snapshot-retention-ms"' in page
    assert 'content="35000"' in page


def test_not_ready_current_keeps_observation_draft_separate_and_private_from_status() -> None:
    drafts = UnifiedFreezeDraftIndex()
    draft = replace(
        _freeze(),
        items=(
            replace(
                _freeze().items[0],
                action=RecommendationAction.OBSERVE,
                reason="near_score_threshold",
            ),
        ),
    )
    assert drafts.publish(draft).accepted
    queries = UnifiedFreezeQueries(UnifiedFreezeIndex(), drafts, _Repository(), _Clock())
    app = CreateApp(
        services=UnifiedWebServices(
            queries,
            UnifiedFreezeEventStream(),
            lambda: {"status": "running", "phase": "morning_main"},
        )
    )
    client = app.test_client()

    current = client.get("/api/freezes/tomorrow/current")
    status = client.get("/api/status")

    assert current.status_code == 200
    assert current.get_json()["status"] == "not_ready"
    assert current.get_json()["items"] == []
    assert [item["code"] for item in current.get_json()["draft"]["items"]] == ["600000"]
    assert [item["code"] for item in current.get_json()["draft"]["top_scores"]] == ["600000"]
    assert current.get_json()["draft"]["input_versions"]["score_scale"] == ("weighted_evidence_quality_0_100")
    assert current.headers["ETag"] == f'"{draft.version}"'
    assert draft.version not in status.get_data(as_text=True)


def test_unified_sse_replays_cursor_and_status_exposes_stream_health() -> None:
    app, queries, stream = _app()
    stream.publish_committed(BuildFreezeCommitted(_freeze()))
    client = app.test_client()

    response = client.get("/api/events", headers={"Last-Event-ID": "0"}, buffered=False)
    iterator = iter(response.response)
    assert next(iterator).decode() == ": connected\n\n"
    event = next(iterator).decode()
    response.close()
    status = client.get("/api/status").get_json()

    assert status["schema_version"] == "runtime_status"
    assert status["release"] == {
        "freeze_view_schema": "freeze_view",
    }
    assert "event: freeze" in event
    freeze_patch = json.loads(event.split("data: ", 1)[1])
    assert freeze_patch["strategy"] == "tomorrow"
    assert freeze_patch["patch_schema_version"] == 4
    assert freeze_patch["publication_id"] == _freeze().version
    assert freeze_patch["replace"] is True
    assert freeze_patch["coverage"] == {
        "candidate_count": 11,
        "evaluated_count": 1,
        "rejected_count": 10,
        "selected_count": 1,
        "executable_count": 1,
        "observation_count": 0,
        "unavailable_count": 0,
    }
    assert freeze_patch["coverage"] == client.get("/api/freezes/tomorrow/current").get_json()["coverage"]
    assert "filtered_count" not in freeze_patch
    assert freeze_patch["input_versions"]["score_model"] == ("daily_reconstructible_ensemble:model-hash")
    assert [item["code"] for item in freeze_patch["upserts"]] == ["600000"]
    assert freeze_patch["upserts"][0]["scores"]["predicted_net_excess_pct"] == 1.25
    assert status["events"]["sequence"] == 1
    assert status["strategies"]["tomorrow"]["status"] == queries.current(Strategy.TOMORROW).status
    assert status["runtime_version"] == "runtime:test"
    assert status["company_research"] == {
        "state": "running",
        "tracked_output_codes": 12,
        "tracked_strategies": 2,
        "trade_date": NOW.date().isoformat(),
    }
    assert status["scheduler"]["strategy_errors"] == {}
    assert status["deepseek"] == {
        "enabled": True,
        "configured": False,
        "physical_attempts": 0,
        "zero_call_reason": "api_key_missing",
    }
    assert status["deepseek_budget"]["limit"] == 168
    assert status["market_data"] == {
        "latest_source": "sina",
        "market_epoch": "market:merge-22",
        "reference_epoch": "reference:fixture",
        "candidate_quote_age": {
            "latest_source_time": NOW.isoformat(),
            "p50_seconds": 1.0,
            "p95_seconds": 2.0,
            "sample_count": 120,
        },
        "candidate_quote_cache_entries": 120,
        "candidate_quote_latest_source": "tencent",
        "history_coverage_ratio": 0.5,
        "history_covered_rows": 60,
        "history_universe_rows": 120,
        "history_error_count": 3,
        "published_history_state": "published",
        "published_history_snapshot_hash": "b" * 64,
        "published_history_data_cutoff": "2026-09-21",
        "history_maintenance_state": "already_current",
        "history_maintenance_reason": None,
        "history_maintenance_stage": "publishing_snapshot",
        "history_maintenance_completed_units": 120,
        "history_maintenance_total_units": 120,
        "issuer_eligibility": {
            "excluded_count": 3,
            "fact_count": 4,
            "integrity_ok": True,
            "last_error": None,
            "manifest_hash": "a" * 64,
            "persistence_error_count": 0,
            "reason_counts": {"historical_st": 3},
            "schema_version": "issuer_eligibility_registry",
        },
        "market_feature_rows": 5567,
        "market_changes": {
            "dirty": 12,
            "inserted": 1,
            "merge_epoch": "merge-22",
            "removed": 2,
            "updated": 9,
        },
        "market_quote_age": {
            "latest_source_time": NOW.isoformat(),
            "maximum_seconds": 5.0,
            "p50_seconds": 2.0,
            "p95_seconds": 4.0,
            "sample_count": 5567,
        },
        "measured_at": NOW.isoformat(),
        "latency_waterfall": {
            "pending_trace_count": 1,
            "completed_count": 40,
            "dropped_count": 0,
            "dropped_stage_count": 0,
            "failed_count": 2,
            "planned_count": 44,
            "sample_capacity": 512,
            "stage_capacity": 64,
            "stages": {
                "freeze_publish": {
                    "maximum_ms": 4.0,
                    "p50_ms": 1.0,
                    "p95_ms": 3.0,
                    "sample_count": 40,
                }
            },
            "superseded_count": 1,
            "timeout_count": 0,
            "trace_capacity": 512,
        },
        "security_master": {
            "complete_rows": 850,
            "listing_age_rows": 850,
            "listing_date_rows": 850,
            "persistence_schedule_error_count": 0,
            "provider": "free_market+production_calendar",
            "total_rows": 851,
            "tushare_required": False,
        },
        "sources": {
            "baostock_industry": {
                "data_age_seconds": 45.0,
                "enabled": True,
                "error_count": 1,
                "invalid_rows": 331,
                "last_error_code": "TimeoutError",
                "last_latency_ms": 120000.0,
                "planned_count": 2,
                "snapshot_rows": 5219,
                "success_count": 1,
                "timeout_count": 1,
                "timeout_seconds": 120.0,
            },
            "exchange": {
                "data_age_seconds": 30.0,
                "enabled": True,
                "error_count": 1,
                "last_latency_ms": 9702.0,
                "listing_date_rows": 5212,
                "p50_latency_ms": 9702.0,
                "p95_latency_ms": 9702.0,
                "planned_count": 2,
                "snapshot_rows": 5212,
                "success_count": 1,
                "timeout_count": 0,
                "timeout_seconds": 15.0,
            },
            "sina": {
                "circuit_open": False,
                "data_age_seconds": 2.0,
                "error_count": 0,
                "last_latency_ms": 600.0,
                "p50_latency_ms": 550.0,
                "p95_latency_ms": 700.0,
                "planned_count": 4,
                "success_count": 4,
                "timeout_count": 0,
            },
            "tushare": {
                "access_points": 120,
                "process_api_attempts_last_minute": 1,
                "process_api_attempts_today": 1,
                "circuit_open": False,
                "daily_call_limit": 8000,
                "data_age_seconds": 5.0,
                "enabled": True,
                "error_count": 0,
                "history_mode": "unadjusted_daily",
                "last_latency_ms": 320.0,
                "local_rate_limit_count": 0,
                "minute_call_limit": 50,
                "p50_latency_ms": 320.0,
                "p95_latency_ms": 320.0,
                "planned_count": 1,
                "process_remaining_calls_today": 7999,
                "success_count": 1,
                "timeout_count": 0,
            },
        },
    }
    assert "canonical_snapshot" not in status["market_data"]
    assert "last_error" not in status["market_data"]["sources"]["sina"]
    assert status["health"] == {"level": "degraded", "issue_count": 1}
    assert status["recent_errors"] == [
        {
            "code": "refresh:source_unavailable",
            "severity": "degraded",
            "strategy": "tomorrow",
            "stage": "refresh",
            "occurred_at": NOW.isoformat(),
            "last_occurred_at": NOW.isoformat(),
            "count": 2,
            "recovery_status": "active",
            "resolved_at": None,
        }
    ]


def test_http_reads_do_not_invoke_external_io() -> None:
    app, _, _ = _app()
    client = app.test_client()

    for path in (
        "/",
        "/api/freezes/tomorrow/current",
        "/api/freezes/tomorrow/dates",
        "/api/status",
    ):
        assert client.get(path).status_code == 200


def test_maintenance_status_projects_six_heads_and_restart_identity_without_io(monkeypatch):
    _, queries, stream = _app()
    heads = []
    for profile in ("v1", "v2", "v3"):
        for strategy in (Strategy.TOMORROW, Strategy.D25):
            loaded = ModelPublicationIdentity(profile, strategy, "a" * 64, "b" * 64, NOW.date(), "c" * 64, "d" * 64)
            disk = replace(loaded, metadata_hash="e" * 64)
            head = ProjectHeadTrainingStatus(
                HeadTrainingStatus(profile, strategy, disk, None, loaded=loaded, owned=profile == "v3")
            )
            head["private_path"] = "must-not-leak"
            head["disk"]["private_path"] = "must-not-leak"
            heads.append(head)
    runtime = {
        "scoring_profile": {"profile_id": "v3", "heads": {}, "maintenance": heads},
        "market_data": {
            "restart_required": True,
            "reference_publication": {
                "loaded_identity": "old",
                "disk_identity": "new",
                "restart_required": True,
                "private_path": "must-not-leak",
            },
        },
    }
    app = CreateApp(services=UnifiedWebServices(queries, stream, lambda: runtime))

    def forbidden(*args, **kwargs):
        raise AssertionError("HTTP status must read only the memory projection")

    monkeypatch.setattr("sqlite3.connect", forbidden)
    monkeypatch.setattr("trader.training.infra.history.history_training_due.EvaluateAllHistoryTrainingDue", forbidden)
    response = app.test_client().get("/api/status")
    payload = response.get_json()
    assert "must-not-leak" not in response.get_data(as_text=True)
    projected = payload["scoring_profile"]["maintenance"]
    assert [(head["profile_id"], head["strategy"]) for head in projected] == [
        ("v1", "tomorrow"),
        ("v1", "d25"),
        ("v2", "tomorrow"),
        ("v2", "d25"),
        ("v3", "tomorrow"),
        ("v3", "d25"),
    ]
    assert [head["restart_required"] for head in projected] == [False, False, False, False, True, True]
    assert all(head["loaded"]["publication_hash"] == "b" * 64 for head in projected)
    assert all(head["disk"]["publication_hash"] == "e" * 64 for head in projected)
    assert payload["market_data"]["reference_publication"]["restart_required"] is True


def _app():
    index = UnifiedFreezeIndex()
    freeze = _freeze()
    assert index.publish(freeze, expected_version=None).accepted
    long_projection = LongProjection(
        NOW.date(),
        1,
        NOW,
        (("watchlist", "watchlist:1"),),
        (
            LongProjectionItem(
                "600001",
                "semiconductor",
                "quote:1",
                name="长期样例",
                industry="半导体设备",
                price=12.3,
                pct_change=1.2,
                source="fixture",
                source_time=NOW,
                quote_status="live",
            ),
        ),
    )
    assert index.publish(long_projection, expected_version=None).accepted
    queries = UnifiedFreezeQueries(index, UnifiedFreezeDraftIndex(), _Repository(), _Clock())
    stream = UnifiedFreezeEventStream()
    services = UnifiedWebServices(
        queries,
        stream,
        lambda: {
            "status": "running",
            "runtime_version": "runtime:test",
            "scheduler": {"strategy_errors": {}},
            "company_research": {
                "state": "running",
                "running_codes": 4,
                "pending_codes": 6,
                "completed_batches": 2,
                "partial_batches": 1,
                "failed_batches": 1,
                "deferred_codes": 4,
                "cooldown_codes": 3,
                "retry_wait_codes": 2,
                "next_retry_seconds": 30.0,
                "gated_offer_codes": 5,
                "short_circuited_batches": 1,
                "short_circuited_codes": 8,
                "tracked_code_gates": 9,
                "evicted_code_gates": 0,
                "last_error": "must-not-leak",
                "batch_size": 4,
                "batch_budget_seconds": 40.0,
                "success_cooldown_seconds": 60.0,
                "retry_delays_seconds": [60.0, 120.0],
                "trade_date": NOW.date().isoformat(),
                "tracked_strategies": 2,
                "tracked_output_codes": 12,
                "next_periodic_at": NOW.isoformat(),
                "intent_offer_count": 2,
                "periodic_offer_count": 1,
                "result_count": 3,
                "rescore_result_count": 2,
                "codes": ["must-not-leak"],
            },
            "deepseek_budget": {"used": 12, "remaining": 156, "planned_limit": 71},
            "market_data": {
                "latest_source": "sina",
                "market_epoch": "market:merge-22",
                "reference_epoch": "reference:fixture",
                "market_feature_rows": 5567,
                "candidate_quote_cache_entries": 120,
                "candidate_quote_latest_source": "tencent",
                "market_quote_age": {
                    "sample_count": 5567,
                    "p50_seconds": 2.0,
                    "p95_seconds": 4.0,
                    "maximum_seconds": 5.0,
                    "latest_source_time": NOW.isoformat(),
                },
                "candidate_quote_age": {
                    "sample_count": 120,
                    "p50_seconds": 1.0,
                    "p95_seconds": 2.0,
                    "maximum_seconds": float("nan"),
                    "latest_source_time": NOW.isoformat(),
                },
                "history_universe_rows": 120,
                "history_covered_rows": 60,
                "history_coverage_ratio": 0.5,
                "history_error_count": 3,
                "published_history_state": "published",
                "published_history_snapshot_hash": "b" * 64,
                "published_history_data_cutoff": "2026-09-21",
                "history_maintenance_state": "already_current",
                "history_maintenance_reason": None,
                "history_maintenance_stage": "publishing_snapshot",
                "history_maintenance_completed_units": 120,
                "history_maintenance_total_units": 120,
                "history_recovery_requested_count": 240,
                "history_recovery_cache_hit_count": 40,
                "history_recovery_dispatched_count": 120,
                "history_recovery_deferred_count": 80,
                "history_recovery_inflight_count": 2,
                "history_recovery_latency_ms": 950,
                "history_tail_requested_count": 120,
                "history_tail_dispatched_count": 2,
                "history_tail_cache_hit_count": 1,
                "history_tail_deferred_count": 117,
                "history_tail_inflight_count": 0,
                "history_tail_expected_date": "2026-09-22",
                "history_tail_quality_counts": {
                    "full_history_ready": 2,
                    "adjustment_conflict": 1,
                    "tail_pending": -1,
                    "raw_only": True,
                    "qfq_only": "bad",
                    "must-not-leak": 3,
                },
                "history_tail_last_error": None,
                "history_tail_codes": ["must-not-leak"],
                "history_recovery_codes": ["must-not-leak"],
                "issuer_eligibility": {
                    "schema_version": "issuer_eligibility_registry",
                    "fact_count": 4,
                    "excluded_count": 3,
                    "reason_counts": {"historical_st": 3, "must-not-leak": "bad"},
                    "manifest_hash": "a" * 64,
                    "integrity_ok": True,
                    "persistence_error_count": 0,
                    "last_error": None,
                    "codes": ["must-not-leak"],
                },
                "market_changes": {
                    "merge_epoch": "merge-22",
                    "inserted": 1,
                    "updated": 9,
                    "removed": 2,
                    "dirty": 12,
                    "internal_codes": ["must-not-leak"],
                },
                "latency_waterfall": {
                    "sample_capacity": 512,
                    "trace_capacity": 512,
                    "stage_capacity": 64,
                    "pending_trace_count": 1,
                    "planned_count": 44,
                    "completed_count": 40,
                    "failed_count": 2,
                    "timeout_count": 0,
                    "superseded_count": 1,
                    "dropped_count": 0,
                    "dropped_stage_count": 0,
                    "stages": {
                        "freeze_publish": {
                            "sample_count": 40,
                            "p50_ms": 1.0,
                            "p95_ms": 3.0,
                            "maximum_ms": 4.0,
                            "samples": ["must-not-leak"],
                        }
                    },
                    "traces": {"must-not-leak": {}},
                },
                "security_master": {
                    "total_rows": 851,
                    "listing_date_rows": 850,
                    "listing_age_rows": 850,
                    "complete_rows": 850,
                    "provider": "free_market+production_calendar",
                    "tushare_required": False,
                    "persistence_schedule_error_count": 0,
                    "internal_payload": "must-not-leak",
                },
                "measured_at": NOW.isoformat(),
                "sources": {
                    "baostock_industry": {
                        "enabled": True,
                        "planned_count": 2,
                        "success_count": 1,
                        "error_count": 1,
                        "timeout_count": 1,
                        "last_latency_ms": 120000.0,
                        "data_age_seconds": 45.0,
                        "snapshot_rows": 5219,
                        "invalid_rows": 331,
                        "last_error_code": "TimeoutError",
                        "timeout_seconds": 120.0,
                        "raw_rows": ["must-not-leak"],
                        "last_error": "must-not-leak",
                    },
                    "exchange": {
                        "enabled": True,
                        "planned_count": 2,
                        "success_count": 1,
                        "error_count": 1,
                        "timeout_count": 0,
                        "last_latency_ms": 9702.0,
                        "p50_latency_ms": 9702.0,
                        "p95_latency_ms": 9702.0,
                        "data_age_seconds": 30.0,
                        "snapshot_rows": 5212,
                        "listing_date_rows": 5212,
                        "timeout_seconds": 15.0,
                        "last_error": "must-not-leak",
                    },
                    "sina": {
                        "planned_count": 4,
                        "success_count": 4,
                        "error_count": 0,
                        "timeout_count": 0,
                        "circuit_open": False,
                        "last_latency_ms": 600.0,
                        "p50_latency_ms": 550.0,
                        "p95_latency_ms": 700.0,
                        "data_age_seconds": 2.0,
                        "last_error": "must-not-leak",
                    },
                    "tushare": {
                        "enabled": True,
                        "access_points": 120,
                        "history_mode": "unadjusted_daily",
                        "minute_call_limit": 50,
                        "daily_call_limit": 8000,
                        "process_api_attempts_last_minute": 1,
                        "process_api_attempts_today": 1,
                        "process_remaining_calls_today": 7999,
                        "local_rate_limit_count": 0,
                        "planned_count": 1,
                        "success_count": 1,
                        "error_count": 0,
                        "timeout_count": 0,
                        "circuit_open": False,
                        "last_latency_ms": 320.0,
                        "p50_latency_ms": 320.0,
                        "p95_latency_ms": 320.0,
                        "data_age_seconds": 5.0,
                        "token": "must-not-leak",
                    },
                },
                "canonical_snapshot": {"missing_reasons": {"600001.price": "must-not-leak"}},
            },
            "deepseek": {
                "enabled": True,
                "configured": False,
                "last_physical_attempts": 0,
                "api_key": "must-not-leak",
                "physical_call_acceptance": {
                    "zero_call_reason": "api_key_missing",
                    "external_payload": "must-not-leak",
                },
            },
            "health": {"level": "degraded", "issue_count": 1},
            "recent_errors": [
                {
                    "code": "refresh:source_unavailable",
                    "severity": "degraded",
                    "strategy": "tomorrow",
                    "stage": "refresh",
                    "occurred_at": NOW.isoformat(),
                    "last_occurred_at": NOW.isoformat(),
                    "count": 2,
                    "recovery_status": "active",
                    "resolved_at": None,
                    "external_payload": "must-not-leak",
                }
            ],
        },
    )
    return CreateApp(services=services), queries, stream


def _freeze() -> ScoredFreeze:
    pipeline_keys = PIPELINE_STAGE_KEYS
    return ScoredFreeze(
        Strategy.TOMORROW,
        NOW.date(),
        1,
        NOW,
        "local",
        None,
        (
            ("market", "market:1"),
            ("score_scale", "weighted_evidence_quality_0_100"),
            ("score_model", "daily_reconstructible_ensemble:model-hash"),
        ),
        "config:1",
        "strategy:1",
        "fusion:1",
        (
            FreezeItem(
                "600000",
                RecommendationAction.EXECUTABLE,
                True,
                1,
                88.0,
                84.0,
                84.0,
                (
                    ("local_score", 84.0),
                    ("model_prediction_rank", 84.0),
                ),
                (),
                "threshold_met",
                Board.MAIN,
                1,
                "浦发银行",
                "银行",
                FreezeQuote(
                    "600000",
                    10.25,
                    2.5,
                    1_000_000_000.0,
                    0.8,
                    300_000_000_000.0,
                    "fixture",
                    NOW,
                    "quote:1",
                ),
                model_diagnostics=FreezeModelDiagnostics(84.0, 1.45, 0.2, 1.25, 0.1),
            ),
        ),
        (("hard_filter", 10),),
        pipeline=RecommendationPipelineStatus(
            "action_publish",
            tuple(PipelineStageStatus(key, "completed", 1, 1) for key in pipeline_keys),
        ),
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
