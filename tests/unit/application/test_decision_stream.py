from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from trader.application.decision_events import build_v2_decision_committed
from trader.application.decision_stream import UnifiedDecisionEventStream
from trader.domain.recommendation.decision_identity import DecisionItem, ScoredDecision
from trader.domain.recommendation.models import RecommendationAction, Strategy

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
