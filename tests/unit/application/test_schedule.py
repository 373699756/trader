from __future__ import annotations

from datetime import datetime

import pytest

from trader.application.runtime.schedule import (
    SHANGHAI,
    MarketPhase,
    decision_at,
    freeze_due_at,
    phase_at,
    seconds_until_next_schedule_boundary,
    startup_freeze_strategies,
)


@pytest.mark.parametrize(
    ("clock", "expected"),
    [
        ("09:14:59", MarketPhase.CLOSED),
        ("09:15:00", MarketPhase.WARMUP),
        ("09:30:00", MarketPhase.TODAY_OBSERVE),
        ("09:35:59", MarketPhase.TODAY_OBSERVE),
        ("09:36:00", MarketPhase.TODAY_MAIN),
        ("10:30:00", MarketPhase.TODAY_LATE),
        ("11:20:00", MarketPhase.MIDDAY),
        ("13:00:00", MarketPhase.AFTERNOON),
        ("14:20:00", MarketPhase.FINAL_REVIEW),
        ("14:48:00", MarketPhase.DEEPSEEK_CUTOFF),
        ("14:49:50", MarketPhase.FINAL_QUOTE),
        ("14:50:00", MarketPhase.FROZEN),
        ("15:00:00", MarketPhase.AFTER_CLOSE),
    ],
)
def test_phase_boundaries_are_left_closed(clock, expected) -> None:
    at = datetime.fromisoformat(f"2026-07-16T{clock}").replace(tzinfo=SHANGHAI)
    assert phase_at(at, is_trading_day=True) is expected


def test_freeze_decisions_are_exact_windows() -> None:
    today = datetime(2026, 7, 16, 11, 20, tzinfo=SHANGHAI)
    afternoon = datetime(2026, 7, 16, 14, 50, tzinfo=SHANGHAI)

    assert decision_at(today, is_trading_day=True).freeze_strategies == ("today",)
    assert decision_at(afternoon, is_trading_day=True).freeze_strategies == ("tomorrow", "d25")
    assert decision_at(today, is_trading_day=False).freeze_strategies == ()


def test_freeze_due_survives_a_missed_exact_window() -> None:
    midday = datetime(2026, 7, 16, 13, 0, tzinfo=SHANGHAI)
    after_freeze = datetime(2026, 7, 16, 15, 0, tzinfo=SHANGHAI)

    assert decision_at(midday, is_trading_day=True).freeze_strategies == ()
    assert freeze_due_at(midday, is_trading_day=True) == ("today",)
    assert freeze_due_at(after_freeze, is_trading_day=True) == ("today", "tomorrow", "d25")
    assert freeze_due_at(after_freeze, is_trading_day=False) == ()


def test_scheduler_wakes_at_deepseek_submission_cutoffs() -> None:
    before_today_cutoff = datetime(2026, 7, 16, 11, 17, 59, tzinfo=SHANGHAI)
    before_afternoon_cutoff = datetime(2026, 7, 16, 14, 45, 59, tzinfo=SHANGHAI)

    assert seconds_until_next_schedule_boundary(before_today_cutoff, maximum_seconds=60) == 1
    assert seconds_until_next_schedule_boundary(before_afternoon_cutoff, maximum_seconds=60) == 1


def test_deepseek_cutoff_keeps_local_scoring_open_without_model_review() -> None:
    cutoff = datetime(2026, 7, 16, 14, 49, 20, tzinfo=SHANGHAI)

    decision = decision_at(cutoff, is_trading_day=True)

    assert decision.should_score is True
    assert decision.should_review is False
    assert seconds_until_next_schedule_boundary(cutoff.replace(second=19), maximum_seconds=60) == 1


@pytest.mark.parametrize(
    ("clock", "expected"),
    (
        ("11:19:59", ()),
        ("11:20:00", ()),
        ("14:49:59", ()),
        ("14:50:00", ("tomorrow", "d25")),
        ("14:59:59", ("tomorrow", "d25")),
        ("15:00:00", ()),
        ("19:30:00", ()),
    ),
)
def test_cold_start_freeze_recovery_never_includes_today(clock: str, expected: tuple[str, ...]) -> None:
    started_at = datetime.fromisoformat(f"2026-07-16T{clock}").replace(tzinfo=SHANGHAI)

    assert startup_freeze_strategies(started_at, is_trading_day=True) == expected
    assert startup_freeze_strategies(started_at, is_trading_day=False) == ()
