from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime, timedelta
from itertools import cycle
from typing import cast

import pytest

from trader.application.publisher import SnapshotPublisher, SubscriberLimitError, encode_sse
from trader.domain.market.models import FeatureSnapshot
from trader.domain.recommendation.models import (
    FusionMode,
    LiveOverlay,
    Recommendation,
    RecommendationAction,
    RecommendationSnapshot,
    ScoreBreakdown,
    Strategy,
)

ApplicationFeatureFactory = Callable[[str, datetime], FeatureSnapshot]


def _recommendation(
    code: str,
    observed_at: datetime,
    application_feature_factory: ApplicationFeatureFactory,
) -> Recommendation:
    return Recommendation(
        strategy=Strategy.TODAY,
        features=application_feature_factory(code, observed_at),
        score=ScoreBreakdown({}, 80.0, 0.0, 80.0, None, 0.0, 0.0, 80.0, FusionMode.LOCAL_DEGRADED, False),
        local_risk_facts=(),
        deepseek_risk_facts=(),
        review=None,
        action=RecommendationAction.OBSERVE,
        action_reason="test",
        veto=False,
    )


def _snapshot(
    snapshot_id: str,
    utc_now: datetime,
    recommendations: tuple[Recommendation, ...],
) -> RecommendationSnapshot:
    return RecommendationSnapshot(
        snapshot_id=snapshot_id,
        strategy=Strategy.TODAY,
        trade_date="2026-07-16",
        phase="today_main",
        data_version="v1",
        strategy_version="s1",
        fusion_version="f1",
        fusion_mode=FusionMode.LOCAL_DEGRADED,
        published_at=utc_now,
        recommendations=recommendations,
        filtered_count=0,
        filter_reasons={},
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


def test_publisher_emits_overlay_without_republishing_snapshot(utc_now: datetime) -> None:
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

    assert event.event_type == "overlay_patch"
    assert event.data["patch_schema_version"] == 2
    assert event.data["schema_version"] == 2
    assert event.data["projection_version"] == "frozen-1"
    assert event.data["snapshot_id"] == "frozen-1"
    assert event.data["overlay_version"] == "overlay-1"


def test_publisher_reports_bounded_sse_and_today_score_latency(
    utc_now: datetime,
    application_feature_factory: ApplicationFeatureFactory,
) -> None:
    measured_at = utc_now
    monotonic = iter((10.0, 10.075))
    publisher = SnapshotPublisher(
        history_size=2,
        client_queue_size=2,
        now=lambda: measured_at,
        monotonic=lambda: next(monotonic),
    )
    recommendation = _recommendation("600001", utc_now - timedelta(seconds=10), application_feature_factory)
    snapshot = _snapshot("today-1", utc_now - timedelta(seconds=2), (recommendation,))

    publisher.publish(snapshot)

    event = publisher.events_after(0)
    assert event is not None
    assert event[0].event_type == "recommendation_patch"
    assert event[0].data["patch_schema_version"] == 2
    assert event[0].data["schema_version"] == 2
    assert event[0].data["projection_version"] == "today-1"
    assert event[0].data["base_projection_version"] is None
    assert event[0].data["removed_codes"] == []
    upserts = cast(list[Mapping[str, object]], event[0].data["upserts"])
    assert upserts[0]["code"] == "600001"

    status = publisher.status()
    assert status["sse_publish_latency"] == {
        "sample_count": 1,
        "p50_seconds": 2.0,
        "p95_seconds": 2.0,
        "maximum_seconds": 2.0,
        "target_seconds": 2.0,
        "meets_target": True,
    }
    assert status["sse_enqueue_latency"] == {
        "sample_count": 1,
        "p50_ms": 75.0,
        "p95_ms": 75.0,
        "maximum_ms": 75.0,
        "target_ms": 100.0,
        "meets_target": True,
    }
    assert status["today_score_publish_latency"] == {
        "sample_count": 1,
        "p50_seconds": 10.0,
        "p95_seconds": 10.0,
        "maximum_seconds": 10.0,
        "target_seconds": 15.0,
        "meets_target": True,
    }


def test_publisher_emits_incremental_snapshot_patch_after_base_snapshot(
    utc_now: datetime,
    application_feature_factory: ApplicationFeatureFactory,
) -> None:
    publisher = SnapshotPublisher(history_size=4, client_queue_size=2, now=lambda: utc_now)
    unchanged = _recommendation("600001", utc_now, application_feature_factory)
    removed = _recommendation("600002", utc_now, application_feature_factory)
    base = _snapshot("today-base", utc_now, (unchanged, removed))
    changed = replace(unchanged, rank=2)
    inserted = _recommendation("600003", utc_now, application_feature_factory)
    next_snapshot = _snapshot("today-next", utc_now, (changed, inserted))

    publisher.publish(base)
    publisher.publish(next_snapshot)

    events = publisher.events_after(0)
    assert events is not None
    initial_patch = events[0].data
    incremental_patch = events[1].data
    initial_upserts = cast(list[Mapping[str, object]], initial_patch["upserts"])
    incremental_upserts = cast(list[Mapping[str, object]], incremental_patch["upserts"])
    assert initial_patch["replace"] is True
    assert [item["code"] for item in initial_upserts] == ["600001", "600002"]
    assert incremental_patch["replace"] is False
    assert incremental_patch["base_projection_version"] == "today-base"
    assert incremental_patch["projection_version"] == "today-next"
    assert incremental_patch["etag"] == "today-next:2026-07-16:live"
    assert [item["code"] for item in incremental_upserts] == ["600001", "600003"]
    assert incremental_patch["removed_codes"] == ["600002"]


def test_publisher_enqueue_latency_reports_failed_internal_target(
    utc_now: datetime,
    application_feature_factory: ApplicationFeatureFactory,
) -> None:
    monotonic = cycle((20.0, 20.101))
    publisher = SnapshotPublisher(
        history_size=2,
        client_queue_size=2,
        now=lambda: utc_now,
        monotonic=lambda: next(monotonic),
    )

    publisher.publish(_snapshot("today-slow", utc_now, ()))

    latency = cast(Mapping[str, object], publisher.status()["sse_enqueue_latency"])
    assert latency["p95_ms"] == 101.0
    assert latency["target_ms"] == 100.0
    assert latency["meets_target"] is False


def test_publisher_does_not_emit_same_day_draft_after_frozen_projection(
    utc_now: datetime,
) -> None:
    publisher = SnapshotPublisher(history_size=4, client_queue_size=2, now=lambda: utc_now)
    draft = _snapshot("today-draft", utc_now, ())
    frozen = replace(draft, snapshot_id="today-frozen", frozen=True, phase="frozen")
    late_draft = replace(draft, snapshot_id="today-late", phase="today_late")

    publisher.publish(draft)
    frozen_event = publisher.publish(frozen)
    late_event = publisher.publish(late_draft)

    assert frozen_event is not None
    assert late_event is None
    assert publisher.last_sequence() == frozen_event.sequence
    assert publisher.status()["rejected_late_drafts"] == 1


def test_publisher_rejects_same_day_frozen_replacements(utc_now: datetime) -> None:
    publisher = SnapshotPublisher(history_size=4, client_queue_size=2, now=lambda: utc_now)
    frozen = replace(_snapshot("today-frozen", utc_now, ()), frozen=True, phase="frozen")
    changed = replace(frozen, filtered_count=frozen.filtered_count + 1)
    replacement = replace(frozen, snapshot_id="today-replacement")

    frozen_event = publisher.publish(frozen)
    changed_event = publisher.publish(changed)
    replacement_event = publisher.publish(replacement)

    assert frozen_event is not None
    assert changed_event is None
    assert replacement_event is None
    assert publisher.last_sequence() == frozen_event.sequence
    assert publisher.status()["rejected_frozen_replacements"] == 2


def test_publisher_rejects_older_snapshot_without_emitting_event(utc_now: datetime) -> None:
    publisher = SnapshotPublisher(history_size=4, client_queue_size=2, now=lambda: utc_now)
    current = _snapshot("today-current", utc_now, ())
    older = replace(current, snapshot_id="today-older", trade_date="2026-07-15")

    current_event = publisher.publish(current)
    older_event = publisher.publish(older)

    assert current_event is not None
    assert older_event is None
    assert publisher.last_sequence() == current_event.sequence
    assert publisher.status()["rejected_older_snapshots"] == 1
