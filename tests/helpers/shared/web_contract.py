from __future__ import annotations
from datetime import date, datetime
from zoneinfo import ZoneInfo
from trader.http_api.route_services import UnifiedWebServices
from trader.recommendation.application.pipeline.freeze_publish.draft_index import UnifiedFreezeDraftIndex
from trader.recommendation.application.pipeline.freeze_publish.event_stream import UnifiedFreezeEventStream
from trader.recommendation.application.pipeline.freeze_publish.read_only_queries import UnifiedFreezeQueries
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import UnifiedFreezeIndex
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
from trader.web import CreateApp

NOW = datetime(2026, 8, 11, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai"))

PIPELINE_STAGE_KEYS = PIPELINE_STAGE_ORDER


class _Clock:
    def now(self) -> datetime:
        return NOW


class _FreezeRecords:
    def load(self, strategy: Strategy, trade_date: date):
        return None

    def list_dates(self, strategy: Strategy, *, limit: int = 31) -> tuple[date, ...]:
        return (date(2026, 8, 8),) if strategy is Strategy.TOMORROW else ()


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
                selection_date=NOW.date(),
                selection_price=10.0,
                selection_to_now_pct=23.0,
            ),
        ),
    )
    assert index.publish(long_projection, expected_version=None).accepted
    queries = UnifiedFreezeQueries(index, UnifiedFreezeDraftIndex(), _FreezeRecords(), _Clock())
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
                "history_maintenance_elapsed_seconds": 201.5,
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
                    "pending_persistence_count": 2,
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
                    "cninfo_industry": {
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
