from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from zoneinfo import ZoneInfo

from tests.unit.domain.test_freeze_identity import stage_snapshots
from trader.http_api.response.freeze_projection import SerializeEvent
from trader.recommendation.application.pipeline.freeze_publish.event_stream import UnifiedFreezeEventStream
from trader.recommendation.application.pipeline.freeze_publish.freeze_events import BuildFreezeCommitted
from trader.recommendation.domain.evidence.pipeline import (
    PIPELINE_STAGE_ORDER,
    PipelineStageStatus,
    RecommendationPipelineStatus,
)
from trader.recommendation.domain.market.models import Board
from trader.recommendation.domain.publication.freeze_identity import (
    FreezeItem,
    FreezeOverlay,
    FreezeQuote,
    ScoredFreeze,
)
from trader.recommendation.domain.publication.models import RecommendationAction, Strategy

NOW = datetime(2026, 8, 11, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai"))


def test_unified_stream_replays_monotonic_cross_strategy_events() -> None:
    stream = UnifiedFreezeEventStream(history_size=3)
    today = stream.publish_committed(BuildFreezeCommitted(_freeze(Strategy.TOMORROW, 1)))
    tomorrow = stream.publish_committed(BuildFreezeCommitted(_freeze(Strategy.TOMORROW, 1)))

    subscription = stream.open_subscription(today.sequence)

    assert tomorrow.sequence == today.sequence + 1
    assert subscription.resync_reason is None
    assert subscription.replay == (tomorrow,)
    assert tomorrow.payload.strategy is Strategy.TOMORROW
    stream.unsubscribe(subscription.queue)


def test_profiled_batch_is_one_full_replacement_event() -> None:
    stream = UnifiedFreezeEventStream()
    events = tuple(
        (
            profile,
            (
                BuildFreezeCommitted(_freeze(Strategy.TOMORROW, sequence)),
                BuildFreezeCommitted(_freeze(Strategy.D25, sequence + 1)),
            ),
        )
        for profile, sequence in (("v1", 1), ("v2", 3), ("v3", 5), ("v5", 7))
    )

    published = stream.publish_batch(11, events)
    payload = SerializeEvent(published)

    assert published.event_type == "batch"
    assert stream.last_sequence() == 1
    assert payload["round_sequence"] == 11
    assert set(payload["profiles"]) == {"v1", "v2", "v3", "v5"}
    assert all(set(profile) == {"tomorrow", "d25"} for profile in payload["profiles"].values())


def test_scored_freeze_event_serializes_complete_replace_patch_without_snapshot_get() -> None:
    base = _freeze(Strategy.TOMORROW, 1)
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
    freeze = replace(
        base,
        pipeline=RecommendationPipelineStatus(
            "action_publish",
            tuple(PipelineStageStatus(key, "completed", 2, 2) for key in keys),
            stage_snapshots(NOW, count=2),
        ),
        items=(
            replace(base.items[0], rank=2, selection_rank=2),
            replace(
                base.items[0],
                code="600001",
                action=RecommendationAction.UNAVAILABLE,
                selected=True,
                rank=1,
                selection_rank=1,
                candidate_score=95.0,
                local_score=91.0,
                final_score=91.0,
                reason="risk_blocked",
            ),
        ),
    )

    payload = SerializeEvent(UnifiedFreezeEventStream().publish_committed(BuildFreezeCommitted(freeze)))

    assert payload["patch_schema_version"] == 4
    assert payload["replace"] is True
    assert payload["snapshot_id"] == freeze.version
    assert payload["publication_id"] == freeze.version
    assert payload["projection_version"] == freeze.version
    assert payload["removed_codes"] == []
    assert payload["removals"] == []
    assert payload["view"] == "live"
    assert payload["coverage"] == {
        "candidate_count": 12,
        "evaluated_count": 2,
        "rejected_count": 10,
        "selected_count": 2,
        "executable_count": 1,
        "observation_count": 0,
        "unavailable_count": 1,
    }
    assert "filtered_count" not in payload
    assert payload["input_versions"] == {
        "market": "market:1",
        "score_model": "daily_reconstructible_ensemble:model-hash",
    }
    assert payload["pipeline"]["current_stage"] == "action_publish"
    assert len(payload["pipeline"]["stages"]) == len(PIPELINE_STAGE_ORDER)
    assert len(payload["pipeline"]["stage_snapshots"]) == 11
    assert payload["pipeline"]["stage_snapshots"][-1]["stage"] == "action_publish"
    assert [(item["code"], item["rank"], item["tier"]) for item in payload["upserts"]] == [
        ("600001", 1, "avoid"),
        ("600000", 2, "recommend"),
    ]
    assert payload["upserts"][1] == {
        "action": "executable",
        "action_reason": "threshold_met",
        "anchor_price": None,
        "anchor_source_time": None,
        "anchor_daily_return_pct": None,
        "board": "main",
        "code": "600000",
        "downside": None,
        "industry": "",
        "market_cap": None,
        "name": "",
        "price": None,
        "pct_change": None,
        "quote_status": "missing",
        "rank": 2,
        "selected": True,
        "tier": "recommend",
        "selection_rank": 2,
        "research_coverage": None,
        "review_outcome": None,
        "risks": [],
        "scores": {
            "ledger": None,
            "candidate_score": 88.0,
            "deepseek_risk_penalty": None,
            "deepseek_score": None,
            "final_score": 84.0,
            "local_score": 84.0,
            "model_signal_score": None,
            "predicted_excess_return_pct": None,
            "estimated_cost_pct": None,
            "predicted_net_excess_pct": None,
            "model_disagreement_pct": None,
        },
        "setup": None,
        "source": None,
        "source_time": None,
        "turnover_rate": None,
        "amount": None,
    }
    assert [item["code"] for item in payload["top_scores"]] == ["600001", "600000"]


def test_unified_stream_requires_resync_for_expired_and_ahead_cursors() -> None:
    stream = UnifiedFreezeEventStream(history_size=2)
    for sequence in range(1, 4):
        stream.publish_committed(BuildFreezeCommitted(_freeze(Strategy.TOMORROW, sequence)))

    expired = stream.open_subscription(0)
    ahead = stream.open_subscription(99)

    assert expired.resync_reason == "cursor_expired"
    assert ahead.resync_reason == "cursor_ahead"
    stream.unsubscribe(expired.queue)
    stream.unsubscribe(ahead.queue)


def test_unified_stream_drops_slow_client_without_blocking_publication() -> None:
    stream = UnifiedFreezeEventStream(client_queue_size=1)
    subscription = stream.open_subscription(0)

    stream.publish_committed(BuildFreezeCommitted(_freeze(Strategy.D25, 1)))
    stream.publish_committed(BuildFreezeCommitted(_freeze(Strategy.D25, 2)))

    assert stream.is_subscribed(subscription.queue) is False
    assert stream.status().slow_subscriber_drops == 1
    assert stream.last_sequence() == 2


def test_unified_stream_publishes_explicit_identity_resync() -> None:
    stream = UnifiedFreezeEventStream()

    event = stream.publish_resync("identity_mismatch")

    assert event.event_type == "resync_required"
    assert event.payload.reason == "identity_mismatch"


def test_overlay_event_serializes_row_patch_with_parent_and_projection_identities() -> None:
    freeze = _freeze(Strategy.TOMORROW, 1)
    quote = FreezeQuote("600000", 10.2, 1.2, 100.0, 2.0, 1_000.0, "fixture", NOW, "quote:2")
    overlay = FreezeOverlay(Strategy.TOMORROW, NOW.date(), freeze.version, NOW, (quote,), sequence=1)

    event = UnifiedFreezeEventStream().publish_overlay(
        overlay,
        parent_version=freeze.version,
    )
    payload = SerializeEvent(event)

    assert payload["snapshot_id"] == freeze.version
    assert payload["publication_id"] == freeze.version
    assert payload["projection_version"] != freeze.version
    assert payload["schema_version"] == "freeze_event"
    assert payload["patch_schema_version"] == 4
    assert payload["quotes"] == [
        {
            "code": "600000",
            "price": 10.2,
            "pct_change": 1.2,
            "amount": 100.0,
            "turnover_rate": 2.0,
            "market_cap": 1_000.0,
            "source": "fixture",
            "source_time": NOW.isoformat(),
            "quote_status": "live",
        }
    ]


def _freeze(strategy: Strategy, sequence: int) -> ScoredFreeze:
    item = FreezeItem(
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
        Board.MAIN,
        1,
    )
    return ScoredFreeze(
        strategy,
        date(2026, 8, 11),
        sequence,
        NOW,
        "local",
        None,
        (
            ("market", f"market:{sequence}"),
            ("score_model", "daily_reconstructible_ensemble:model-hash"),
        ),
        "config:1",
        "strategy:1",
        "fusion:1",
        (item,),
        (("hard_filter", 10),),
    )
