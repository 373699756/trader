from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from trader.application.tomorrow_events import TomorrowDecisionEventStream
from trader.application.tomorrow_views import (
    TomorrowDecisionView,
    TomorrowLiveQuote,
    TomorrowQuoteOverlay,
    TomorrowViewIdentity,
)

NOW = datetime(2026, 7, 28, 14, 45, tzinfo=ZoneInfo("Asia/Shanghai"))


def test_event_stream_uses_monotonic_ids_and_explicit_cursor_replay() -> None:
    stream = TomorrowDecisionEventStream(history_size=3)
    first = stream.publish_decision(_view("decision:1"))
    second = stream.publish_decision(_view("decision:2"))

    replay = stream.open_subscription(after_sequence=first.sequence)

    assert second.sequence == first.sequence + 1
    assert replay.resync_reason is None
    assert replay.replay == (second,)
    stream.unsubscribe(replay.queue)


def test_event_stream_without_cursor_starts_at_atomic_current_sequence() -> None:
    stream = TomorrowDecisionEventStream()
    stream.publish_decision(_view("decision:1"))

    subscription = stream.open_subscription(after_sequence=None)

    assert subscription.server_sequence_at_open == 1
    assert subscription.replay == ()
    assert subscription.resync_reason is None
    stream.unsubscribe(subscription.queue)


def test_event_stream_classifies_ahead_expired_and_identity_mismatch() -> None:
    stream = TomorrowDecisionEventStream(history_size=2)
    stream.publish_decision(_view("decision:1"))
    stream.publish_decision(_view("decision:2"))
    stream.publish_decision(_view("decision:3"))

    expired = stream.open_subscription(after_sequence=0)
    ahead = stream.open_subscription(after_sequence=99)
    mismatch = stream.publish_overlay(
        TomorrowQuoteOverlay(
            decision_version="decision:old",
            version="quote:1",
            observed_at=NOW,
            quotes=(TomorrowLiveQuote("600000", 10.0, 1.0, "fixture", NOW, "q:1"),),
        )
    )

    assert expired.resync_reason == "cursor_expired"
    assert ahead.resync_reason == "cursor_ahead"
    assert mismatch.accepted is False
    assert mismatch.reason == "identity_mismatch"
    assert mismatch.event is not None
    assert mismatch.event.event_type == "resync_required"
    stream.unsubscribe(expired.queue)
    stream.unsubscribe(ahead.queue)


def test_slow_subscriber_is_dropped_without_blocking_publisher() -> None:
    stream = TomorrowDecisionEventStream(client_queue_size=1)
    subscription = stream.open_subscription(after_sequence=0)

    stream.publish_decision(_view("decision:1"))
    stream.publish_decision(_view("decision:2"))

    assert stream.is_subscribed(subscription.queue) is False
    assert stream.status().slow_subscriber_drops == 1
    assert stream.last_sequence() == 2


def test_overlay_outside_current_decision_scope_requires_base_resync() -> None:
    stream = TomorrowDecisionEventStream()
    stream.publish_decision(_view("decision:1"))

    result = stream.publish_overlay(
        TomorrowQuoteOverlay(
            decision_version="decision:1",
            version="quote:1",
            observed_at=NOW,
            quotes=(
                TomorrowLiveQuote(
                    "600000",
                    10.0,
                    1.0,
                    "fixture",
                    NOW,
                    "q:1",
                ),
            ),
        )
    )

    assert result.accepted is False
    assert result.reason == "base_mismatch"
    assert result.event is not None
    assert result.event.event_type == "resync_required"


def _view(version: str) -> TomorrowDecisionView:
    return TomorrowDecisionView.ready_identity(
        TomorrowViewIdentity(
            trade_date="2026-07-28",
            projection_version=version,
            decision_version=version,
            market_epoch_version="market:1",
            feature_epoch_version="candidate:1",
            research_epoch_version="research:1",
            config_version="config:1",
            strategy_version="tomorrow:2",
            fusion_version="fusion:2",
            projection_stage="hybrid",
        ),
        published_at=NOW,
        etag=f'"{version}"',
    )
