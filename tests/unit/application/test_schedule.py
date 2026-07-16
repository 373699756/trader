from __future__ import annotations

from datetime import datetime

import pytest

from trader.application.schedule import SHANGHAI, MarketPhase, decision_at, phase_at


@pytest.mark.parametrize(
    ("clock", "expected"),
    [
        ("09:14:59", MarketPhase.CLOSED),
        ("09:15:00", MarketPhase.WARMUP),
        ("09:30:00", MarketPhase.TODAY_OBSERVE),
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
