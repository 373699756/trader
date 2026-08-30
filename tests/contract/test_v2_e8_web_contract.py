from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime
from zoneinfo import ZoneInfo

from trader.application.decision_core import UnifiedDecisionIndex
from trader.application.decision_drafts import UnifiedDecisionDraftIndex
from trader.application.decision_events import build_v2_decision_committed
from trader.application.decision_queries import UnifiedDecisionQueries
from trader.application.decision_stream import UnifiedDecisionEventStream
from trader.domain.market.models import MarketQuote
from trader.domain.recommendation.decision_identity import (
    DecisionItem,
    DecisionModelDiagnostics,
    DecisionQuote,
    ScoredDecision,
)
from trader.domain.recommendation.models import RecommendationAction, Strategy
from trader.web import create_app
from trader.web.route_services import UnifiedWebServices, WebApiConfig
from trader.web.static_assets import WEB_ASSET_REVISION

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
    assert current.get_json()["schema_version"] == "v2_decision_view_v3"
    assert current.get_json()["draft"] is None
    assert current.get_json()["strategy"] == "today"
    assert current.get_json()["items"][0]["name"] == "浦发银行"
    assert current.get_json()["items"][0]["industry"] == "银行"
    assert current.get_json()["items"][0]["scores"]["predicted_net_excess_pct"] == 1.25
    assert current.get_json()["input_versions"]["score_model"] == ("daily_reconstructible_ensemble_v1:model-hash")
    assert current.get_json()["items"][0]["quote"] == {
        "price": 10.25,
        "pct_change": 2.5,
        "amount": 1_000_000_000.0,
        "turnover_rate": 0.8,
        "market_cap": 300_000_000_000.0,
        "source": "fixture",
        "source_time": NOW.isoformat(),
        "status": "decision_anchor",
    }
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


def test_root_injects_configured_web_snapshot_retention_margin() -> None:
    app, queries, stream = _app()
    services = UnifiedWebServices(
        queries,
        stream,
        lambda: {"status": "running", "phase": "today_main"},
        WebApiConfig(heartbeat_seconds=15, snapshot_retention_seconds=35),
    )

    page = create_app(services=services).test_client().get("/").get_data(as_text=True)

    assert 'name="trader-web-snapshot-retention-ms"' in page
    assert 'content="35000"' in page


def test_not_ready_current_keeps_observation_draft_separate_and_private_from_status() -> None:
    drafts = UnifiedDecisionDraftIndex()
    draft = replace(
        _decision(),
        items=(
            replace(
                _decision().items[0],
                action=RecommendationAction.OBSERVE,
                reason="near_score_threshold",
            ),
        ),
    )
    assert drafts.publish(draft).accepted
    queries = UnifiedDecisionQueries(UnifiedDecisionIndex(), drafts, _Repository(), _Clock())
    app = create_app(
        services=UnifiedWebServices(
            queries,
            UnifiedDecisionEventStream(),
            lambda: {"status": "running", "phase": "today_main"},
        )
    )
    client = app.test_client()

    current = client.get("/api/v2/decisions/today/current")
    status = client.get("/api/v2/status")

    assert current.status_code == 200
    assert current.get_json()["status"] == "not_ready"
    assert current.get_json()["items"] == []
    assert [item["code"] for item in current.get_json()["draft"]["items"]] == ["600000"]
    assert current.headers["ETag"] == f'"{draft.content_hash}"'
    assert draft.version not in status.get_data(as_text=True)


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

    assert status["schema_version"] == "v2_status_v7"
    assert status["release"] == {
        "decision_view_schema": "v2_decision_view_v3",
        "web_asset_revision": WEB_ASSET_REVISION,
    }
    assert "event: decision" in event
    decision_patch = json.loads(event.split("data: ", 1)[1])
    assert decision_patch["strategy"] == "today"
    assert decision_patch["patch_schema_version"] == 3
    assert decision_patch["replace"] is True
    assert decision_patch["input_versions"]["score_model"] == ("daily_reconstructible_ensemble_v1:model-hash")
    assert [item["code"] for item in decision_patch["upserts"]] == ["600000"]
    assert decision_patch["upserts"][0]["scores"]["predicted_net_excess_pct"] == 1.25
    assert status["events"]["sequence"] == 1
    assert status["strategies"]["today"]["status"] == queries.current(Strategy.TODAY).status
    assert status["runtime_version"] == "runtime:test"
    assert status["scheduler"]["strategy_errors"] == {}
    assert status["deepseek"] == {
        "enabled": True,
        "configured": False,
        "physical_attempts": 0,
        "zero_call_reason": "api_key_missing",
    }
    assert status["deepseek_budget"]["limit"] == 168
    assert status["market_data"] == {
        "active_source": "sina",
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
        "history_warmup_completed_count": 60,
        "history_warmup_failure_count": 3,
        "history_warmup_inflight_count": 30,
        "history_warmup_retry_deferred_count": 2,
        "history_warmup_unique_failure_count": 2,
        "history_warmup_timeout_count": 1,
        "history_warmup_inflight_age_seconds": 4.5,
        "history_warmup_batch_timeout_seconds": 20.0,
        "history_warmup_last_source": "tencent",
        "history_warmup_planned_count": 120,
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
            "active_trace_count": 1,
            "completed_count": 40,
            "dropped_count": 0,
            "dropped_stage_count": 0,
            "failed_count": 2,
            "planned_count": 44,
            "sample_capacity": 512,
            "stage_capacity": 64,
            "stages": {
                "decision_publish": {
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
        "/api/v2/decisions/today/current",
        "/api/v2/decisions/today/dates",
        "/api/v2/status",
    ):
        assert client.get(path).status_code == 200


def _app():
    index = UnifiedDecisionIndex()
    decision = _decision()
    assert index.publish(decision, expected_version=None).accepted
    queries = UnifiedDecisionQueries(index, UnifiedDecisionDraftIndex(), _Repository(), _Clock())
    stream = UnifiedDecisionEventStream()
    services = UnifiedWebServices(
        queries,
        stream,
        lambda: {
            "status": "running",
            "runtime_version": "runtime:test",
            "scheduler": {"strategy_errors": {}},
            "deepseek_budget": {"used": 12, "remaining": 156, "planned_limit": 71},
            "market_data": {
                "active_source": "sina",
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
                "history_warmup_planned_count": 120,
                "history_warmup_completed_count": 60,
                "history_warmup_failure_count": 3,
                "history_warmup_inflight_count": 30,
                "history_warmup_retry_deferred_count": 2,
                "history_warmup_unique_failure_count": 2,
                "history_warmup_timeout_count": 1,
                "history_warmup_inflight_age_seconds": 4.5,
                "history_warmup_batch_timeout_seconds": 20.0,
                "history_warmup_last_source": "tencent",
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
                    "active_trace_count": 1,
                    "planned_count": 44,
                    "completed_count": 40,
                    "failed_count": 2,
                    "timeout_count": 0,
                    "superseded_count": 1,
                    "dropped_count": 0,
                    "dropped_stage_count": 0,
                    "stages": {
                        "decision_publish": {
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
    return create_app(services=services), queries, stream


def _decision() -> ScoredDecision:
    return ScoredDecision(
        Strategy.TODAY,
        NOW.date(),
        1,
        NOW,
        "local",
        None,
        (
            ("market", "market:1"),
            ("score_model", "daily_reconstructible_ensemble_v1:model-hash"),
        ),
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
                (
                    ("local_score", 84.0),
                    ("model_net_utility_rank", 84.0),
                ),
                (),
                "threshold_met",
                "浦发银行",
                "银行",
                DecisionQuote(
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
                model_diagnostics=DecisionModelDiagnostics(84.0, 1.45, 0.2, 1.25, 0.1),
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
