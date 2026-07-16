from __future__ import annotations

from trader.application.events import BoundedEventQueue, EventPriority, new_event


def test_queue_reserves_capacity_for_risk_and_freeze(utc_now) -> None:
    event_queue = BoundedEventQueue(maximum_size=3, reserved_priority_size=1)
    for index in range(2):
        assert event_queue.put(_event(utc_now, EventPriority.MARKET_QUOTES, f"stock-{index}")) is True
    assert event_queue.put(_event(utc_now, EventPriority.MARKET_QUOTES, "stock-3")) is False
    assert event_queue.put(_event(utc_now, EventPriority.FREEZE, "market")) is True

    assert event_queue.get().priority is EventPriority.FREEZE


def test_queue_coalesces_same_idempotency_key_to_newest_event(utc_now) -> None:
    event_queue = BoundedEventQueue(maximum_size=4, reserved_priority_size=1)
    old = _event(utc_now, EventPriority.MARKET_QUOTES, "market")
    newer = new_event(
        old.event_type,
        subject_key=old.subject_key,
        trade_date=old.trade_date,
        phase=old.phase,
        strategy=old.strategy,
        priority=old.priority,
        data_version=old.data_version,
        config_version=old.config_version,
        created_at=utc_now.replace(second=1),
        payload={"version": 2},
    )
    assert event_queue.put(old) is True
    assert event_queue.put(newer) is True

    received = event_queue.get()
    assert received.event_id == newer.event_id
    assert received.payload["version"] == 2
    assert event_queue.status()["merged_count"] == 1


def _event(at, priority, subject):
    return new_event(
        "quote",
        subject_key=subject,
        trade_date="2026-07-16",
        phase="today_main",
        strategy=None,
        priority=priority,
        data_version="v1",
        config_version="c1",
        created_at=at,
    )
