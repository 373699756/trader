from __future__ import annotations

import pytest

from trader.application.ports.model_scoring import ScoringHeadRuntimeStatus
from trader.application.recommendation.model_scoring_router import ModelScoringRouter
from trader.domain.recommendation.models import Strategy


class _ScoringCapability:
    history_required_sessions = 61

    def __init__(self, profile_id: str, strategy: Strategy) -> None:
        self.profile_id = profile_id
        self.strategy = strategy
        self.eligible_calls = 0
        self.score_calls = 0
        self.status_calls = 0

    def is_input_eligible(self, feature) -> bool:
        del feature
        self.eligible_calls += 1
        return False

    def score(self, features, *, context=None):
        del features, context
        self.score_calls += 1
        return f"{self.strategy.value}-batch"

    def status(self) -> ScoringHeadRuntimeStatus:
        self.status_calls += 1
        return ScoringHeadRuntimeStatus(
            active=True,
            profile_id=self.profile_id,  # type: ignore[arg-type]
            model_id=f"{self.strategy.value}-model",
            model_hash="a" * 64,
            scoring_version="fixture",
            activation_basis="manual_user_override",
            historical_status="historical_data_insufficient",
            historical_failure_reasons=("daily_close_proxy_not_point_in_time",),
            monitoring_mode="automatic_t1_outcome_settlement",
            automatic_model_update=False,
            loss_probability_status="not_modeled",
            runtime_anchor="11:20" if self.strategy is Strategy.TODAY else "14:50",
        )


def test_v1_v2_router_keeps_today_and_d25_on_rule_scoring() -> None:
    tomorrow = _ScoringCapability("v2", Strategy.TOMORROW)
    router = ModelScoringRouter("v2", {Strategy.TOMORROW: tomorrow})

    assert router.history_required_sessions(Strategy.TODAY) == 20
    assert router.history_required_sessions(Strategy.D25) == 20
    assert router.uses_model(Strategy.TODAY) is False
    assert router.uses_model(Strategy.D25) is False
    assert router.score(Strategy.TODAY, ()) is None
    assert router.score(Strategy.D25, ()) is None
    assert router.score(Strategy.TOMORROW, ()) == "tomorrow-batch"


def test_v3_router_delegates_three_independent_head_capabilities() -> None:
    capabilities = {
        strategy: _ScoringCapability("v3", strategy) for strategy in (Strategy.TODAY, Strategy.TOMORROW, Strategy.D25)
    }
    router = ModelScoringRouter("v3", capabilities)

    for strategy in capabilities:
        assert router.uses_model(strategy) is True
        assert router.history_required_sessions(strategy) == 61
        assert router.score(strategy, ()) == f"{strategy.value}-batch"
    status = router.status()
    assert status is not None
    assert status.profile_id == "v3"
    assert tuple(status.heads) == (Strategy.TODAY, Strategy.TOMORROW, Strategy.D25)
    assert all(capability.score_calls == 1 for capability in capabilities.values())
    assert all(capability.status_calls == 2 for capability in capabilities.values())


def test_router_rejects_long_as_a_non_scoring_strategy() -> None:
    router = ModelScoringRouter("v2", {Strategy.TOMORROW: _ScoringCapability("v2", Strategy.TOMORROW)})

    with pytest.raises(ValueError, match="long strategy"):
        router.uses_model(Strategy.LONG)
