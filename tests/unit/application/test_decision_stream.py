from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from trader.application.decision_events import build_v2_decision_committed
from trader.application.decision_stream import UnifiedDecisionEventStream
from trader.domain.recommendation.decision_identity import (
    DecisionItem,
    DecisionOverlay,
    DecisionQuote,
    ScoredDecision,
)
from trader.domain.recommendation.models import RecommendationAction, Strategy
from trader.web.decision_serializers import serialize_event

NOW = datetime(2026, 8, 11, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai"))


def test_unified_stream_replays_monotonic_cross_strategy_events() -> None:
    stream = UnifiedDecisionEventStream(history_size=3)
    today = stream.publish_committed(build_v2_decision_committed(_decision(Strategy.TODAY, 1)))
    tomorrow = stream.publish_committed(build_v2_decision_committed(_decision(Strategy.TOMORROW, 1)))

    subscription = stream.open_subscription(today.sequence)

    assert tomorrow.sequence == today.sequence + 1
    assert subscription.resync_reason is None
    assert subscription.replay == (tomorrow,)
    assert tomorrow.payload.strategy is Strategy.TOMORROW
    stream.unsubscribe(subscription.queue)


def test_scored_decision_event_serializes_complete_replace_patch_without_snapshot_get() -> None:
    decision = _decision(Strategy.TOMORROW, 1)

    payload = serialize_event(UnifiedDecisionEventStream().publish_committed(build_v2_decision_committed(decision)))

    assert payload["patch_schema_version"] == 2
    assert payload["replace"] is True
    assert payload["snapshot_id"] == decision.version
    assert payload["projection_version"] == decision.content_hash
    assert payload["removed_codes"] == []
    assert payload["removals"] == []
    assert payload["view"] == "live"
    assert payload["upserts"] == [
        {
            "action": "executable",
            "action_reason": "threshold_met",
            "anchor_price": None,
            "anchor_source_time": None,
            "code": "600000",
            "downside": None,
            "industry": "",
            "market_cap": None,
            "name": "",
            "price": None,
            "pct_change": None,
            "quote_status": "missing",
            "rank": 1,
            "research_coverage": None,
            "review_outcome": None,
            "risks": [],
            "scores": {
                "candidate_score": 88.0,
                "deepseek_risk_penalty": None,
                "deepseek_score": None,
                "final_score": 84.0,
                "local_score": 84.0,
            },
            "setup": None,
            "source": None,
            "source_time": None,
            "turnover_rate": None,
            "amount": None,
        }
    ]


def test_unified_stream_requires_resync_for_expired_and_ahead_cursors() -> None:
    stream = UnifiedDecisionEventStream(history_size=2)
    for sequence in range(1, 4):
        stream.publish_committed(build_v2_decision_committed(_decision(Strategy.TODAY, sequence)))

    expired = stream.open_subscription(0)
    ahead = stream.open_subscription(99)

    assert expired.resync_reason == "cursor_expired"
    assert ahead.resync_reason == "cursor_ahead"
    stream.unsubscribe(expired.queue)
    stream.unsubscribe(ahead.queue)


def test_unified_stream_drops_slow_client_without_blocking_publication() -> None:
    stream = UnifiedDecisionEventStream(client_queue_size=1)
    subscription = stream.open_subscription(0)

    stream.publish_committed(build_v2_decision_committed(_decision(Strategy.D25, 1)))
    stream.publish_committed(build_v2_decision_committed(_decision(Strategy.D25, 2)))

    assert stream.is_subscribed(subscription.queue) is False
    assert stream.status().slow_subscriber_drops == 1
    assert stream.last_sequence() == 2


def test_unified_stream_publishes_explicit_identity_resync() -> None:
    stream = UnifiedDecisionEventStream()

    event = stream.publish_resync("identity_mismatch")

    assert event.event_type == "resync_required"
    assert event.payload.reason == "identity_mismatch"


def test_overlay_event_serializes_row_patch_with_parent_and_projection_identities() -> None:
    decision = _decision(Strategy.TODAY, 1)
    quote = DecisionQuote("600000", 10.2, 1.2, 100.0, 2.0, 1_000.0, "fixture", NOW, "quote:2")
    overlay = DecisionOverlay(Strategy.TODAY, NOW.date(), decision.version, NOW, (quote,))

    event = UnifiedDecisionEventStream().publish_overlay(
        overlay,
        parent_content_hash=decision.content_hash,
    )
    payload = serialize_event(event)

    assert payload["snapshot_id"] == decision.version
    assert payload["projection_version"] != decision.version
    assert payload["schema_version"] == "v2_event_v1"
    assert payload["patch_schema_version"] == 2
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


def _decision(strategy: Strategy, sequence: int) -> ScoredDecision:
    item = DecisionItem(
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
    )
    return ScoredDecision(
        strategy,
        date(2026, 8, 11),
        sequence,
        NOW,
        "local",
        None,
        (("market", f"market:{sequence}"),),
        "config:1",
        "strategy:1",
        "fusion:1",
        (item,),
        (("hard_filter", 10),),
    )
