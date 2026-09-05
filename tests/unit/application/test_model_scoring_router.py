from __future__ import annotations

import pytest

from trader.application.recommendation.model_scoring_router import ModelScoringRouter
from trader.domain.recommendation.models import Strategy


class _TomorrowScoring:
    history_required_sessions = 61

    def __init__(self) -> None:
        self.eligible_calls = 0
        self.score_calls = 0
        self.status_calls = 0

    def is_input_eligible(self, feature) -> bool:
        del feature
        self.eligible_calls += 1
        return False

    def score(self, features):
        del features
        self.score_calls += 1
        return "tomorrow-batch"

    def status(self):
        self.status_calls += 1
        return "tomorrow-status"


def test_router_keeps_rule_scoring_explicit_for_today_and_d25() -> None:
    tomorrow = _TomorrowScoring()
    router = ModelScoringRouter(tomorrow)

    assert router.history_required_sessions(Strategy.TODAY) == 20
    assert router.history_required_sessions(Strategy.D25) == 20
    assert router.uses_model(Strategy.TODAY) is False
    assert router.uses_model(Strategy.D25) is False
    assert router.score(Strategy.TODAY, ()) is None
    assert router.score(Strategy.D25, ()) is None
    assert tomorrow.score_calls == 0


def test_router_delegates_only_tomorrow_model_capabilities() -> None:
    tomorrow = _TomorrowScoring()
    router = ModelScoringRouter(tomorrow)

    assert router.history_required_sessions(Strategy.TOMORROW) == 61
    assert router.uses_model(Strategy.TOMORROW) is True
    assert router.score(Strategy.TOMORROW, ()) == "tomorrow-batch"
    assert router.status() == "tomorrow-status"
    assert tomorrow.score_calls == 1
    assert tomorrow.status_calls == 1


def test_router_rejects_long_as_a_non_scoring_strategy() -> None:
    router = ModelScoringRouter(_TomorrowScoring())

    with pytest.raises(ValueError, match="long strategy"):
        router.uses_model(Strategy.LONG)
