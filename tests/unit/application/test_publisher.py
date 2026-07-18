from __future__ import annotations

from datetime import timedelta

import pytest

from trader.application.publisher import SnapshotPublisher, SubscriberLimitError, encode_sse
from trader.domain.models import (
    FusionMode,
    LiveOverlay,
    Recommendation,
    RecommendationAction,
    RecommendationSnapshot,
    ScoreBreakdown,
    Strategy,
)


def test_publisher_replays_cursor_and_drops_slow_subscriber() -> None:
    publisher = SnapshotPublisher(history_size=2, client_queue_size=1)
    publisher.subscribe()
    first = publisher.resync("one")
    second = publisher.resync("two")

    assert publisher.status()["subscribers"] == 0
    assert publisher.events_after(first.sequence) == (second,)
    assert "event: resync_required" in encode_sse(second)


def test_publisher_reports_expired_cursor() -> None:
    publisher = SnapshotPublisher(history_size=2, client_queue_size=2)
    publisher.resync("one")
    publisher.resync("two")
    publisher.resync("three")

    assert publisher.events_after(0) is None


def test_publisher_requires_resync_for_cursor_ahead_of_server_sequence() -> None:
    publisher = SnapshotPublisher(history_size=2, client_queue_size=2)
    publisher.resync("one")

    assert publisher.events_after(99) is None
    subscription = publisher.open_subscription(99)
    assert subscription.replay is None
    publisher.unsubscribe(subscription.queue)


def test_publisher_opens_replay_atomically_and_limits_subscribers() -> None:
    publisher = SnapshotPublisher(history_size=2, client_queue_size=2, maximum_subscribers=1)
    first = publisher.resync("one")
    second = publisher.resync("two")

    subscription = publisher.open_subscription(first.sequence)

    assert subscription.replay == (second,)
    with pytest.raises(SubscriberLimitError):
        publisher.open_subscription(second.sequence)
    publisher.unsubscribe(subscription.queue)


def test_publisher_emits_overlay_without_republishing_snapshot(utc_now) -> None:
    publisher = SnapshotPublisher(history_size=2, client_queue_size=2)
    overlay = LiveOverlay(
        snapshot_id="frozen-1",
        strategy=Strategy.TODAY,
        trade_date="2026-07-16",
        version="overlay-1",
        observed_at=utc_now,
        quotes={},
    )

    event = publisher.publish_overlay(overlay)

    assert event.event_type == "live_overlay"
    assert event.data["snapshot_id"] == "frozen-1"
    assert event.data["overlay_version"] == "overlay-1"


def test_publisher_reports_bounded_sse_and_today_score_latency(utc_now, application_feature_factory) -> None:
    measured_at = utc_now
    publisher = SnapshotPublisher(
        history_size=2,
        client_queue_size=2,
        now=lambda: measured_at,
    )
    feature = application_feature_factory("600001", utc_now - timedelta(seconds=10))
    recommendation = Recommendation(
        strategy=Strategy.TODAY,
        features=feature,
        score=ScoreBreakdown({}, 80.0, 0.0, 80.0, None, 0.0, 0.0, 80.0, FusionMode.LOCAL_DEGRADED, False),
        local_risk_facts=(),
        deepseek_risk_facts=(),
        review=None,
        action=RecommendationAction.OBSERVE,
        action_reason="test",
        veto=False,
    )
    snapshot = RecommendationSnapshot(
        snapshot_id="today-1",
        strategy=Strategy.TODAY,
        trade_date="2026-07-16",
        phase="today_main",
        data_version="v1",
        strategy_version="s1",
        fusion_version="f1",
        fusion_mode=FusionMode.LOCAL_DEGRADED,
        published_at=utc_now - timedelta(seconds=2),
        recommendations=(recommendation,),
        filtered_count=0,
        filter_reasons={},
    )

    publisher.publish(snapshot)

    status = publisher.status()
    assert status["sse_publish_latency"] == {
        "sample_count": 1,
        "p50_seconds": 2.0,
        "p95_seconds": 2.0,
        "maximum_seconds": 2.0,
        "meets_target": True,
    }
    assert status["today_score_publish_latency"] == {
        "sample_count": 1,
        "p50_seconds": 10.0,
        "p95_seconds": 10.0,
        "maximum_seconds": 10.0,
        "meets_target": True,
    }
