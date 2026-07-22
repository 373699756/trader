from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from trader.application.cadence import PERIODIC_TASKS, PipelineTask, ScheduledPipelineTask
from trader.application.events import BoundedEventQueue, EventPriority, new_event
from trader.application.pipeline_submission import _scheduled_task_deadline, _scheduled_task_priority
from trader.application.schedule import MarketPhase


def test_realtime_pipeline_balances_live_quotes_and_fifo_dependency_order() -> None:
    dependency_tasks = (PipelineTask.FULL_MARKET, PipelineTask.CANDIDATE_QUOTES, PipelineTask.SCORE)

    assert tuple(task for task in PERIODIC_TASKS if task in dependency_tasks) == dependency_tasks
    assert tuple(_scheduled_task_priority(task) for task in dependency_tasks) == (EventPriority.MARKET_QUOTES,) * 3
    assert _scheduled_task_priority(PipelineTask.TOPK_QUOTES) is EventPriority.LIVE_QUOTES
    assert EventPriority.LIVE_QUOTES < EventPriority.MARKET_QUOTES


def test_dependent_tasks_include_upstream_queue_time_in_expiration_deadline(utc_now) -> None:
    candidate = ScheduledPipelineTask(PipelineTask.CANDIDATE_QUOTES, utc_now, MarketPhase.TODAY_MAIN)
    score = ScheduledPipelineTask(PipelineTask.SCORE, utc_now, MarketPhase.TODAY_MAIN)

    assert _scheduled_task_deadline(candidate) == utc_now + timedelta(seconds=23)
    assert _scheduled_task_deadline(score) == utc_now + timedelta(seconds=38)


def test_queue_reserves_capacity_for_risk_and_freeze(utc_now) -> None:
    event_queue = BoundedEventQueue(maximum_size=3, reserved_priority_size=1)
    for index in range(2):
        assert event_queue.put(_event(utc_now, EventPriority.MARKET_QUOTES, f"60000{index}")) is True
    assert event_queue.put(_event(utc_now, EventPriority.MARKET_QUOTES, "600003")) is False
    assert event_queue.put(_event(utc_now, EventPriority.FREEZE, "market")) is True

    assert event_queue.get().priority is EventPriority.FREEZE


def test_priority_events_arriving_first_cannot_expand_total_capacity(utc_now) -> None:
    event_queue = BoundedEventQueue(maximum_size=3, reserved_priority_size=1)
    assert event_queue.put(_event(utc_now, EventPriority.FREEZE, "market", data_version="freeze-v1")) is True
    assert event_queue.put(_event(utc_now, EventPriority.RISK, "600001", data_version="risk-v1")) is True
    assert event_queue.put(_event(utc_now, EventPriority.MARKET_QUOTES, "600002", data_version="quote-v1")) is True

    assert event_queue.put(_event(utc_now, EventPriority.MARKET_QUOTES, "600003", data_version="quote-v1")) is False
    assert event_queue.status()["depth"] == 3


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


def test_full_queue_replaces_older_quote_version_for_same_subject(utc_now) -> None:
    event_queue = BoundedEventQueue(maximum_size=3, reserved_priority_size=1)
    old = _event(utc_now, EventPriority.MARKET_QUOTES, "600001", data_version="quote-v1")
    other = _event(utc_now, EventPriority.MARKET_QUOTES, "600002", data_version="quote-v1")
    newest = _event(
        utc_now + timedelta(seconds=1),
        EventPriority.MARKET_QUOTES,
        "600001",
        data_version="quote-v2",
    )

    assert event_queue.put(old) is True
    assert event_queue.put(other) is True
    assert event_queue.put(newest) is True

    received = (event_queue.get(), event_queue.get())
    assert {event.event_id for event in received if event is not None} == {other.event_id, newest.event_id}
    status = event_queue.status()
    assert status == {
        "capacity": 3,
        "reserved_priority_capacity": 1,
        "depth": 0,
        "heap_depth": 0,
        "heap_storage_depth": 0,
        "merged_count": 1,
        "rejected_count": 0,
        "replayed_count": 0,
        "closed": False,
    }


def test_full_queue_collapses_all_older_versions_for_subject(utc_now) -> None:
    event_queue = BoundedEventQueue(maximum_size=4, reserved_priority_size=1)
    first = _event(utc_now, EventPriority.MARKET_QUOTES, "600001", data_version="quote-v1")
    second = _event(
        utc_now + timedelta(seconds=1),
        EventPriority.MARKET_QUOTES,
        "600001",
        data_version="quote-v2",
    )
    other = _event(utc_now, EventPriority.MARKET_QUOTES, "600002", data_version="quote-v1")
    newest = _event(
        utc_now + timedelta(seconds=2),
        EventPriority.MARKET_QUOTES,
        "600001",
        data_version="quote-v3",
    )
    for event in (first, second, other, newest):
        assert event_queue.put(event) is True

    received = tuple(event_queue.get() for _index in range(2))
    assert {event.event_id for event in received if event is not None} == {other.event_id, newest.event_id}
    assert event_queue.empty() is True


def test_queue_reports_replay_and_rejects_after_close(utc_now) -> None:
    event_queue = BoundedEventQueue(maximum_size=2, reserved_priority_size=1)
    event_queue.record_replayed()
    event_queue.close()

    assert event_queue.put(_event(utc_now, EventPriority.FREEZE, "market")) is False
    assert event_queue.status()["replayed_count"] == 1
    assert event_queue.status()["rejected_count"] == 1


def test_event_rejects_non_market_non_stock_subject(utc_now) -> None:
    with pytest.raises(ValueError, match="subject_key"):
        _event(utc_now, EventPriority.CANDIDATE_QUOTES, "stock-1")


@pytest.mark.parametrize(
    "payload",
    ({}, {"freeze_strategies": []}, {"freeze_strategies": ["long"]}),
)
def test_freeze_event_requires_non_long_strategy_payload(utc_now, payload) -> None:
    with pytest.raises(ValueError, match="freeze_strategies"):
        new_event(
            "freeze",
            subject_key="market",
            trade_date="2026-07-16",
            phase="midday",
            strategy=None,
            priority=EventPriority.FREEZE,
            data_version="tick:112000",
            config_version="c1",
            created_at=utc_now,
            payload=payload,
        )


@pytest.mark.parametrize("field", ("created_at", "deadline"))
def test_event_rejects_naive_business_time(utc_now, field) -> None:
    values = {"created_at": utc_now, "deadline": utc_now + timedelta(minutes=1)}
    values[field] = datetime(2026, 7, 16, 10, 0)

    with pytest.raises(ValueError, match="timezone-aware"):
        new_event(
            "quote",
            subject_key="market",
            trade_date="2026-07-16",
            phase="today_main",
            strategy=None,
            priority=EventPriority.MARKET_QUOTES,
            data_version="v1",
            config_version="c1",
            created_at=values["created_at"],
            deadline=values["deadline"],
        )


def test_queue_applies_complete_business_priority_order(utc_now) -> None:
    event_queue = BoundedEventQueue(maximum_size=9, reserved_priority_size=2)
    priorities = tuple(reversed(tuple(EventPriority)))
    for index, priority in enumerate(priorities):
        subject = "market" if index == 0 else f"6000{index:02d}"
        assert event_queue.put(_event(utc_now, priority, subject, data_version=f"v{index}")) is True

    received = tuple(event_queue.get() for _priority in priorities)
    assert tuple(event.priority for event in received if event is not None) == tuple(EventPriority)


def test_event_audit_fields_and_idempotency_key_are_complete(utc_now) -> None:
    event = _event(utc_now, EventPriority.MARKET_QUOTES, "market", data_version="quote-v3")

    assert event.idempotency_key == "2026-07-16:today_main:shared:quote:market:quote-v3"
    assert set(event.audit_record(status="pending")) == {
        "event_id",
        "event_type",
        "subject_key",
        "trade_date",
        "phase",
        "strategy",
        "priority",
        "data_version",
        "config_version",
        "status",
        "created_at",
        "deadline",
        "retry_count",
        "payload",
        "error",
    }


def test_event_payload_is_deeply_owned_and_audit_record_is_json_shaped(utc_now) -> None:
    freeze_strategies = ["today"]
    nested = {"codes": ["600001"]}
    event = new_event(
        "freeze",
        subject_key="market",
        trade_date="2026-07-16",
        phase="midday",
        strategy=None,
        priority=EventPriority.FREEZE,
        data_version="tick:112000",
        config_version="c1",
        created_at=utc_now,
        payload={"freeze_strategies": freeze_strategies, "nested": nested},
    )

    freeze_strategies.append("tomorrow")
    nested["codes"].append("600002")

    assert event.payload["freeze_strategies"] == ("today",)
    assert event.payload["nested"] == {"codes": ("600001",)}
    assert event.audit_record(status="pending")["payload"] == {
        "freeze_strategies": ["today"],
        "nested": {"codes": ["600001"]},
    }


@pytest.mark.parametrize("value", (float("nan"), float("inf"), float("-inf")))
def test_event_payload_rejects_non_finite_json_numbers(utc_now, value) -> None:
    with pytest.raises(ValueError, match="finite"):
        new_event(
            "quote",
            subject_key="market",
            trade_date="2026-07-16",
            phase="today_main",
            strategy=None,
            priority=EventPriority.MARKET_QUOTES,
            data_version="v1",
            config_version="c1",
            created_at=utc_now,
            payload={"value": value},
        )


def test_repeated_coalescing_keeps_physical_heap_bounded(utc_now) -> None:
    event_queue = BoundedEventQueue(maximum_size=4, reserved_priority_size=1)
    latest = _event(utc_now, EventPriority.MARKET_QUOTES, "market")
    assert event_queue.put(latest) is True
    for index in range(1, 101):
        latest = new_event(
            latest.event_type,
            subject_key=latest.subject_key,
            trade_date=latest.trade_date,
            phase=latest.phase,
            strategy=latest.strategy,
            priority=latest.priority,
            data_version=latest.data_version,
            config_version=latest.config_version,
            created_at=utc_now + timedelta(seconds=index),
        )
        assert event_queue.put(latest) is True

    status = event_queue.status()
    assert status["depth"] == 1
    assert status["heap_depth"] == 1
    assert status["heap_storage_depth"] <= 8
    assert event_queue.get().event_id == latest.event_id


def _event(at, priority, subject, *, data_version="v1"):
    return new_event(
        "quote",
        subject_key=subject,
        trade_date="2026-07-16",
        phase="today_main",
        strategy=None,
        priority=priority,
        data_version=data_version,
        config_version="c1",
        created_at=at,
    )
