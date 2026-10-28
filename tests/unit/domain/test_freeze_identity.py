from __future__ import annotations

import json
from dataclasses import fields, replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from trader.recommendation.domain.evidence.pipeline import (
    PIPELINE_STAGE_ORDER,
    PIPELINE_STAGES,
    PipelineStageSnapshot,
    PipelineStageStatus,
    RecommendationPipelineStatus,
    SourceHealth,
    SourceHealthState,
    StageState,
)
from trader.recommendation.domain.market.models import Board
from trader.recommendation.domain.market.research import ResearchComponentCoverage, ResearchComponentsFromObservation
from trader.recommendation.domain.publication.freeze_identity import (
    CommittedFreezeRecord,
    FormalScoredFreeze,
    FreezeDownside,
    FreezeItem,
    FreezeModelDiagnostics,
    FreezeOverlay,
    FreezeQuote,
    FreezeResearchCoverage,
    FreezeRiskDisclosure,
    LongProjection,
    LongProjectionItem,
    ScoredFreeze,
    SelectionDiagnostics,
)
from trader.recommendation.domain.publication.historical_pipeline import HistoricalPipelineAudit
from trader.recommendation.domain.publication.models import RecommendationAction, Strategy
from trader.recommendation.infra.persistence.freeze_record_codec import (
    CommittedRecordBytes,
    CommittedRecordFromBytes,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 11, 14, 40, tzinfo=SHANGHAI)


def pipeline() -> RecommendationPipelineStatus:
    keys = (
        "local_data",
        "static_filter",
        "dynamic_market",
        "dynamic_filter",
        "market_features",
        "model_filter",
        "local_score",
        "local_risk",
        "fixed_selection",
        "risk_review",
        "action_publish",
    )
    return RecommendationPipelineStatus(
        "action_publish",
        tuple(PipelineStageStatus(key, "completed", 1, 1) for key in keys),
    )


def freeze(
    strategy: Strategy = Strategy.TOMORROW,
    *,
    sequence: int = 1,
    stage: str = "local",
    parent_version: str | None = None,
    score: float = 88.0,
) -> ScoredFreeze:
    return ScoredFreeze(
        strategy=strategy,
        trade_date=NOW.date(),
        sequence=sequence,
        observed_at=NOW,
        stage=stage,
        parent_version=parent_version,
        input_versions=(("market", "market-initial"), ("daily", "daily-current")),
        config_version="config-current",
        strategy_version="strategy-current",
        fusion_version="fusion-current",
        items=(
            FreezeItem(
                code="600001",
                action=RecommendationAction.EXECUTABLE,
                selected=True,
                rank=1,
                candidate_score=84.0,
                local_score=score if stage == "local" else 88.0,
                final_score=score,
                score_components=(("trend", 88.0),),
                risk_codes=(),
                reason="selected",
                board=Board.MAIN,
                selection_rank=1,
                quote=FreezeQuote(
                    "600001",
                    10.5,
                    1.2,
                    120_000_000.0,
                    2.1,
                    12_000_000_000.0,
                    "tencent",
                    NOW - timedelta(minutes=5),
                    "quote-fixture",
                ),
            ),
        ),
        filter_aggregates=(("st_or_delisting", 2),),
    )


def test_scored_identity_is_canonical_for_all_three_scored_strategies() -> None:
    for strategy in (Strategy.TOMORROW, Strategy.TOMORROW, Strategy.D25):
        first = freeze(strategy)
        reordered = replace(first, input_versions=tuple(reversed(first.input_versions)))

        assert reordered.content_hash == first.content_hash
        assert reordered.version == first.version
        assert reordered.strategy is strategy


def test_formal_record_round_trip_preserves_complete_recommendation_pipeline() -> None:
    current = replace(freeze(), pipeline=pipeline())
    formal = FormalScoredFreeze(current)
    record = CommittedFreezeRecord(formal, NOW + timedelta(minutes=10), "scheduled")

    encoded = CommittedRecordBytes(record)
    restored = CommittedRecordFromBytes(encoded)

    assert isinstance(restored.freeze.pipeline, HistoricalPipelineAudit)
    assert restored.freeze.content_hash == record.freeze.content_hash
    assert CommittedRecordBytes(restored) == encoded
    assert json.loads(encoded)["freeze"]["pipeline"]["current_stage"] == "action_publish"
    assert len(json.loads(encoded)["freeze"]["pipeline"]["stages"]) == len(PIPELINE_STAGE_ORDER)


def stage_snapshots(observed_at: datetime = NOW, *, count: int = 1) -> tuple[PipelineStageSnapshot, ...]:
    return tuple(
        PipelineStageSnapshot(
            stage,
            index + 1,
            f"batch-{index}",
            f"batch-{index + 1}",
            observed_at,
            StageState.READY,
            count,
            count,
            0,
            0,
            0,
            (),
            SourceHealth(SourceHealthState.READY, 1, 1, observed_at, 0),
            10,
            False,
        )
        for index, stage in enumerate(PIPELINE_STAGES)
    )


@pytest.mark.parametrize("operation,latency", (("cache_reuse", 0), ("startup_reuse", None)))
def test_eleven_stage_audit_survives_formal_codec_and_participates_in_hash(operation, latency) -> None:
    snapshots = stage_snapshots()
    snapshots = (
        replace(
            snapshots[0],
            as_of=NOW - timedelta(hours=1),
            latency_ms=latency,
            operation_durations=((operation, latency),),
        ),
        replace(snapshots[1], as_of=NOW - timedelta(hours=1), latency_ms=None),
        *snapshots[2:],
    )
    current = replace(freeze(), pipeline=replace(pipeline(), stage_snapshots=snapshots, elapsed_ms=1250))
    record = CommittedFreezeRecord(FormalScoredFreeze(current), NOW.replace(hour=15, minute=0), "scheduled")
    restored = CommittedRecordFromBytes(CommittedRecordBytes(record))
    assert isinstance(restored.freeze.pipeline, HistoricalPipelineAudit)
    assert restored.version == record.version and restored.payload_hash == record.payload_hash
    from trader.http_api.response.freeze_projection import _serialize_pipeline

    assert _serialize_pipeline(current.pipeline) == _serialize_pipeline(restored.freeze.pipeline)
    assert CommittedRecordBytes(restored) == CommittedRecordBytes(record)
    assert tuple(item.stage.value for item in restored.freeze.pipeline.stage_snapshots) == tuple(
        item.stage.value for item in snapshots
    )
    changed = replace(
        current,
        pipeline=replace(current.pipeline, stage_snapshots=(replace(snapshots[0], latency_ms=11), *snapshots[1:])),
    )
    assert changed.content_hash != current.content_hash
    legacy = replace(freeze(), pipeline=pipeline())
    legacy_record = CommittedFreezeRecord(FormalScoredFreeze(legacy), record.committed_at, "scheduled")
    encoded = CommittedRecordBytes(legacy_record)
    assert "stage_snapshots" not in json.loads(encoded)["freeze"]["pipeline"]
    assert CommittedRecordBytes(CommittedRecordFromBytes(encoded)) == encoded


def test_legacy_formal_record_without_pipeline_keeps_original_identity() -> None:
    record = CommittedFreezeRecord(freeze(), NOW + timedelta(minutes=10), "scheduled")
    encoded = CommittedRecordBytes(record)

    restored = CommittedRecordFromBytes(encoded)

    assert "pipeline" not in json.loads(encoded)["freeze"]
    assert restored == record
    assert restored.freeze.pipeline is None


def test_formal_record_preserves_component_coverage_and_risk_origin() -> None:
    assert ResearchComponentsFromObservation(None) == ResearchComponentCoverage(
        "unknown", "unknown", "unknown", "unknown"
    )
    original = freeze()
    item = replace(
        original.items[0],
        risk_codes=("risk_example",),
        risk_details=(
            FreezeRiskDisclosure("risk_example", "local", True),
            FreezeRiskDisclosure("risk_example", "deepseek", False),
        ),
        research_coverage=FreezeResearchCoverage(
            2,
            1,
            True,
            ResearchComponentCoverage("known_clear", "stale", "unknown", "known_risk"),
        ),
    )
    record = CommittedFreezeRecord(replace(original, items=(item,)), NOW + timedelta(minutes=10), "scheduled")
    restored = CommittedRecordFromBytes(CommittedRecordBytes(record))

    assert restored == record
    assert restored.freeze.items[0].risk_details == item.risk_details
    assert restored.freeze.items[0].research_coverage == item.research_coverage


def test_formal_record_without_new_disclosures_keeps_original_bytes() -> None:
    original = freeze()
    record = CommittedFreezeRecord(original, NOW + timedelta(minutes=10), "scheduled")
    encoded = CommittedRecordBytes(record)

    assert b'"risk_details"' not in encoded
    assert b'"components"' not in encoded
    assert CommittedRecordBytes(CommittedRecordFromBytes(encoded)) == encoded


def test_scored_identity_rejects_long_and_invalid_hybrid_parent_or_local_score() -> None:
    with pytest.raises(ValueError, match="scored strategy"):
        freeze(Strategy.LONG)
    with pytest.raises(ValueError, match="hybrid.*parent"):
        freeze(stage="hybrid")
    with pytest.raises(ValueError, match="local freeze"):
        current = freeze()
        replace(current, items=(replace(current.items[0], final_score=87.99999999),))


def test_scored_identity_accepts_structured_downside_action_reason_but_rejects_free_text() -> None:
    item = freeze().items[0]

    guarded = replace(item, action=RecommendationAction.OBSERVE, reason="downside_guard:trend_breakdown")
    guarded_freeze = replace(freeze(), items=(guarded,))
    record = CommittedFreezeRecord(guarded_freeze, NOW + timedelta(minutes=10), "scheduled")
    restored = CommittedRecordFromBytes(CommittedRecordBytes(record))

    assert restored.freeze.items[0].reason == "downside_guard:trend_breakdown"
    with pytest.raises(ValueError, match="reason must be structured"):
        replace(item, reason="下行保护命中")


def test_long_projection_has_no_scoring_fields_and_stable_identity() -> None:
    projection = LongProjection(
        trade_date=NOW.date(),
        sequence=1,
        observed_at=NOW,
        input_versions=(("quotes", "quotes-fixture"),),
        items=(
            LongProjectionItem(
                "600001",
                "group:001",
                "quote-fixture",
                name="甲公司",
                industry="设备",
                price=10.5,
                pct_change=1.2,
                amount=100_000_000.0,
                turnover_rate=2.0,
                market_cap=10_000_000_000.0,
                source="tencent",
                source_time=NOW,
                quote_status="live",
            ),
        ),
    )

    assert projection.strategy is Strategy.LONG
    assert "score" not in {item.name for item in fields(LongProjectionItem)}
    assert projection.items[0].price == 10.5
    assert projection.items[0].quote_status == "live"
    assert projection.version.startswith("projection:long:")


def test_long_projection_preserves_watchlist_order_and_allows_missing_placeholders() -> None:
    projection = LongProjection(
        trade_date=NOW.date(),
        sequence=1,
        observed_at=NOW,
        input_versions=(("watchlist", "watchlist-fixture"),),
        items=(
            LongProjectionItem("600002", "group:002", "missing:watchlist-fixture"),
            LongProjectionItem("600001", "group:001", "missing:watchlist-fixture"),
        ),
    )

    assert tuple(item.code for item in projection.items) == ("600002", "600001")
    assert all(item.quote_status == "missing" and item.price is None for item in projection.items)


def test_overlay_and_formal_record_validate_parent_time_scope_and_hash() -> None:
    current = freeze()
    quote = FreezeQuote(
        "600001",
        10.6,
        1.3,
        130_000_000.0,
        2.2,
        12_500_000_000.0,
        "tencent",
        NOW,
        "quote-fixture",
    )
    overlay = FreezeOverlay(
        strategy=Strategy.TOMORROW,
        trade_date=NOW.date(),
        parent_version=current.version,
        observed_at=NOW,
        quotes=(quote,),
        sequence=1,
    )
    record = CommittedFreezeRecord(current, NOW + timedelta(minutes=10), "scheduled")

    assert overlay.version.startswith(f"overlay:{current.version}:")
    assert len(record.payload_hash) == 64
    with pytest.raises(ValueError, match="future quote"):
        replace(overlay, quotes=(replace(quote, source_time=NOW + timedelta(seconds=1)),))
    with pytest.raises(ValueError, match="trade date"):
        replace(record, committed_at=NOW + timedelta(days=1))


def test_formal_record_round_trip_preserves_optional_distinct_coverage() -> None:
    item = replace(freeze().items[0], name="浦发银行", industry="银行")
    current = replace(freeze(), items=(item,), population_count=82, rejected_count=81)
    record = CommittedFreezeRecord(current, NOW + timedelta(minutes=10), "scheduled")

    encoded = CommittedRecordBytes(record)
    restored = CommittedRecordFromBytes(encoded)
    encoded_item = json.loads(encoded)["freeze"]["items"][0]

    assert restored.freeze.population_count == 82
    assert restored.freeze.rejected_count == 81
    assert restored.freeze.items[0].name == "浦发银行"
    assert restored.freeze.items[0].industry == "银行"
    assert encoded_item["board"] == "main"
    assert encoded_item["selection_rank"] == 1
    assert restored.freeze.items[0].quote == item.quote
    assert restored.payload_hash == record.payload_hash


def test_formal_record_round_trip_preserves_display_metadata_and_selection_diagnostics() -> None:
    item = replace(
        freeze().items[0],
        setup_type="breakout",
        downside=FreezeDownside("observe", ("trend_breakdown",), 2.0, 1.5, -8.0),
        review_outcome="applied",
        research_coverage=FreezeResearchCoverage(3, 2, True),
        model_diagnostics=FreezeModelDiagnostics(88.0, -0.8, 0.3, -1.1, 0.2),
    )
    current = replace(
        freeze(),
        items=(item,),
        selection_diagnostics=SelectionDiagnostics(88.0, 78.0, 70.0, 6, 6, 1, 0, 1, evaluated_count=37),
    )
    record = CommittedFreezeRecord(current, NOW + timedelta(minutes=10), "scheduled")

    restored = CommittedRecordFromBytes(CommittedRecordBytes(record))
    encoded = json.loads(CommittedRecordBytes(record))

    assert restored == record
    assert restored.freeze.items[0].downside == item.downside
    assert restored.freeze.items[0].model_diagnostics == item.model_diagnostics
    assert restored.freeze.selection_diagnostics == current.selection_diagnostics
    assert encoded["freeze"]["selection_diagnostics"]["evaluated_count"] == 37


def test_formal_record_codec_keeps_pre_aggregate_selection_diagnostics_compatible() -> None:
    current = replace(
        freeze(),
        selection_diagnostics=SelectionDiagnostics(88.0, 78.0, 70.0, 6, 6, 1, 0, 1),
    )
    record = CommittedFreezeRecord(current, NOW + timedelta(minutes=10), "scheduled")

    encoded = CommittedRecordBytes(record)
    restored = CommittedRecordFromBytes(encoded)

    assert "evaluated_count" not in json.loads(encoded)["freeze"]["selection_diagnostics"]
    assert restored == record
    assert restored.freeze.selection_diagnostics is not None
    assert restored.freeze.selection_diagnostics.evaluated_count is None


def test_selection_diagnostics_rejects_inconsistent_evaluated_aggregates() -> None:
    with pytest.raises(ValueError, match="maximum score must match"):
        SelectionDiagnostics(97.44, 78.0, 73.0, 6, 6, 0, 0, 0, evaluated_count=0)
    with pytest.raises(ValueError, match="counts cannot exceed"):
        SelectionDiagnostics(97.44, 78.0, 73.0, 6, 6, 0, 0, 2, evaluated_count=1)


def test_formal_projection_retains_scored_leader_even_when_it_is_avoided() -> None:
    current = replace(
        freeze(),
        items=(
            replace(
                freeze().items[0],
                action=RecommendationAction.UNAVAILABLE,
                selected=True,
                rank=1,
                selection_rank=1,
            ),
        ),
        selection_diagnostics=SelectionDiagnostics(
            97.44,
            78.0,
            73.0,
            6,
            6,
            0,
            0,
            0,
            "no_positive_net_utility",
            evaluated_count=239,
        ),
    )

    formal = FormalScoredFreeze(current)
    restored = CommittedRecordFromBytes(
        CommittedRecordBytes(CommittedFreezeRecord(formal, NOW + timedelta(minutes=10), "scheduled"))
    )

    assert len(formal.items) == 1
    assert formal.items[0].selected is True
    assert restored.freeze.items == formal.items
    assert restored.freeze.selection_diagnostics is not None
    assert restored.freeze.selection_diagnostics.evaluated_count == 239


@pytest.mark.parametrize("location", ["record", "freeze", "item"])
def test_formal_record_codec_rejects_unknown_fields(location: str) -> None:
    record = CommittedFreezeRecord(freeze(), NOW + timedelta(minutes=10), "scheduled")
    payload = json.loads(CommittedRecordBytes(record))
    target = {
        "record": payload,
        "freeze": payload["freeze"],
        "item": payload["freeze"]["items"][0],
    }[location]
    target["legacy_field"] = "must-not-be-ignored"

    with pytest.raises(ValueError, match="unknown fields"):
        CommittedRecordFromBytes(json.dumps(payload).encode())


def test_freeze_quote_rejects_wrong_code_future_time_and_invalid_market_values() -> None:
    current = freeze()
    item = current.items[0]
    assert item.quote is not None

    with pytest.raises(ValueError, match="quote code"):
        replace(item, quote=replace(item.quote, code="600002"))
    with pytest.raises(ValueError, match="future quote"):
        replace(current, items=(replace(item, quote=replace(item.quote, source_time=NOW + timedelta(seconds=1))),))
    with pytest.raises(ValueError, match="quote amount"):
        replace(item.quote, amount=-1.0)
