from __future__ import annotations

from datetime import date, datetime, timedelta

from trader.application.schedule import SHANGHAI
from trader.application.trading_session import (
    CalendarState,
    TradingSessionTracker,
)


class MutableCalendar:
    def __init__(self, *, open_day: bool = True, failure: Exception | None = None) -> None:
        self.open_day = open_day
        self.failure = failure
        self.calls = 0

    def is_trading_day(self, _day: date) -> bool:
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return self.open_day


def test_calendar_failure_is_recorded_without_raising_and_uses_retry_backoff() -> None:
    now = datetime(2026, 7, 16, 9, 15, tzinfo=SHANGHAI)
    calendar = MutableCalendar(failure=RuntimeError("network unavailable"))
    tracker = TradingSessionTracker(now)

    first = tracker.refresh(now, calendar.is_trading_day)
    before_retry = tracker.refresh(now + timedelta(seconds=29), calendar.is_trading_day)
    second = tracker.refresh(now + timedelta(seconds=30), calendar.is_trading_day)

    assert first.calendar_state is CalendarState.UNAVAILABLE
    assert first.next_retry_at == now + timedelta(seconds=30)
    assert before_retry == first
    assert second.next_retry_at == now + timedelta(seconds=90)
    assert calendar.calls == 2


def test_weekend_and_holiday_are_market_closed_not_calendar_unavailable() -> None:
    now = datetime(2026, 7, 18, 10, 0, tzinfo=SHANGHAI)
    tracker = TradingSessionTracker(now)

    status = tracker.refresh(now, MutableCalendar(open_day=False).is_trading_day)

    assert status.calendar_state is CalendarState.AVAILABLE
    assert status.is_trading_day is False
    assert status.phase == "closed"


def test_valid_calendar_result_recovers_from_unavailable_state() -> None:
    now = datetime(2026, 7, 16, 9, 15, tzinfo=SHANGHAI)
    calendar = MutableCalendar(failure=OSError("offline"))
    tracker = TradingSessionTracker(now)
    tracker.refresh(now, calendar.is_trading_day)
    calendar.failure = None

    recovered = tracker.refresh(now + timedelta(seconds=30), calendar.is_trading_day)

    assert recovered.calendar_state is CalendarState.AVAILABLE
    assert recovered.is_trading_day is True
    assert recovered.next_retry_at is None


def test_calendar_retry_backoff_is_30_60_120_300_and_then_capped() -> None:
    current = datetime(2026, 7, 16, 9, 15, tzinfo=SHANGHAI)
    calendar = MutableCalendar(failure=OSError("offline"))
    tracker = TradingSessionTracker(current)
    observed_delays = []

    for _attempt in range(5):
        status = tracker.refresh(current, calendar.is_trading_day)
        assert status.next_retry_at is not None
        observed_delays.append((status.next_retry_at - current).total_seconds())
        current = status.next_retry_at

    assert observed_delays == [30.0, 60.0, 120.0, 300.0, 300.0]


def test_valid_local_calendar_cache_is_explicitly_observable() -> None:
    now = datetime(2026, 7, 16, 9, 15, tzinfo=SHANGHAI)
    tracker = TradingSessionTracker(now)

    status = tracker.refresh(now, MutableCalendar().is_trading_day, cache_used=True)

    assert status.calendar_state is CalendarState.CACHED
    assert status.is_trading_day is True


def test_wall_clock_rollback_rotates_generation_and_rejects_old_completion() -> None:
    start = datetime(2026, 7, 16, 10, 0, tzinfo=SHANGHAI)
    tracker = TradingSessionTracker(start)
    initial = tracker.observe_clock(start, monotonic_seconds=100.0, planned_interval_seconds=5.0)
    rotated = tracker.observe_clock(
        start - timedelta(seconds=2),
        monotonic_seconds=101.0,
        planned_interval_seconds=5.0,
    )

    assert rotated.generation == initial.generation + 1
    assert rotated.discontinuity_reason == "wall_clock_rollback"
    assert tracker.accepts(initial.generation, initial.trade_date) is False
    assert tracker.accepts(rotated.generation, rotated.trade_date) is True


def test_sleep_gap_and_midnight_rotation_have_deterministic_reasons() -> None:
    start = datetime(2026, 7, 16, 13, 0, tzinfo=SHANGHAI)
    tracker = TradingSessionTracker(start)
    tracker.observe_clock(start, monotonic_seconds=10.0, planned_interval_seconds=5.0)

    sleep = tracker.observe_clock(
        start + timedelta(hours=2),
        monotonic_seconds=7210.0,
        planned_interval_seconds=5.0,
    )
    next_day = tracker.observe_clock(
        datetime(2026, 7, 17, 9, 15, tzinfo=SHANGHAI),
        monotonic_seconds=8010.0,
        planned_interval_seconds=5.0,
    )

    assert sleep.discontinuity_reason == "scheduler_gap"
    assert next_day.trade_date == "2026-07-17"
    assert next_day.discontinuity_reason == "trade_date_changed"


def test_trade_date_refresh_notifies_rotation_hook_once() -> None:
    start = datetime(2026, 7, 16, 23, 59, tzinfo=SHANGHAI)
    tracker = TradingSessionTracker(start)
    rotations = []
    tracker.add_rotation_hook(rotations.append)

    status = tracker.refresh(
        datetime(2026, 7, 17, 9, 15, tzinfo=SHANGHAI),
        MutableCalendar().is_trading_day,
    )

    assert [item.generation for item in rotations] == [status.generation]
    assert rotations[0].discontinuity_reason == "trade_date_changed"
