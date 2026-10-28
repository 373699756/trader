from __future__ import annotations

from datetime import datetime

import pytest

from trader.recommendation.application.ports.runtime import CycleRequest, ResearchIntent
from trader.recommendation.application.runtime.schedule import SHANGHAI
from trader.recommendation.domain.publication.models import Strategy
from trader.training.application.research_runtime import ResearchRuntime

NOW = datetime(2026, 8, 21, 10, 0, tzinfo=SHANGHAI)


def _request(strategy: Strategy = Strategy.TOMORROW) -> CycleRequest:
    return CycleRequest(
        strategy,
        NOW.date(),
        NOW,
        "morning_main",
        1,
        f"input:{strategy.value}",
        True,
        NOW.replace(hour=14, minute=48),
    )


def test_published_freezes_only_update_research_visibility_without_starting_network_io() -> None:
    runtime = ResearchRuntime()
    runtime.start()
    try:
        intent = ResearchIntent(
            Strategy.TOMORROW,
            NOW.date(),
            ("600001",),
            ("600001", "600002", "600003"),
        )
        assert runtime.observe(intent, _request()) is False
        status = runtime.status()
    finally:
        runtime.stop(wait=True)

    assert status.tracked_strategies == 1
    assert status.tracked_output_codes == 1


def test_research_intent_must_match_the_published_cycle() -> None:
    runtime = ResearchRuntime()
    intent = ResearchIntent(Strategy.D25, NOW.date(), ("600001",), ("600001",))
    with pytest.raises(ValueError, match="must match"):
        runtime.observe(intent, _request(Strategy.TOMORROW))
