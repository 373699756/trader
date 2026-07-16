from __future__ import annotations

import pytest

from trader.application.publisher import SnapshotPublisher, SubscriberLimitError, encode_sse


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
