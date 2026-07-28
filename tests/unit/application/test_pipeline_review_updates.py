from __future__ import annotations

from datetime import datetime

from trader.application.pipeline_review_updates import review_enabled_for_strategy_phase
from trader.application.pipeline_stages import review_deadline
from trader.application.schedule import SHANGHAI, MarketPhase
from trader.domain.recommendation.models import Strategy


def test_tomorrow_and_d25_review_are_enabled_during_morning_scoring() -> None:
    for phase in (MarketPhase.TODAY_MAIN, MarketPhase.TODAY_LATE):
        assert review_enabled_for_strategy_phase(Strategy.TOMORROW, phase) is True
        assert review_enabled_for_strategy_phase(Strategy.D25, phase) is True


def test_review_acceptance_deadlines_remain_at_freeze_guard_boundaries() -> None:
    morning = datetime(2026, 7, 16, 10, 0, tzinfo=SHANGHAI)
    afternoon = datetime(2026, 7, 16, 14, 0, tzinfo=SHANGHAI)

    assert review_deadline(morning, MarketPhase.TODAY_MAIN).isoformat() == "2026-07-16T11:20:00+08:00"
    assert review_deadline(afternoon, MarketPhase.AFTERNOON).isoformat() == "2026-07-16T14:48:00+08:00"
