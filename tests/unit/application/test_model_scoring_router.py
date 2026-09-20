from __future__ import annotations

import pytest

from trader.recommendation.application.pipeline.local_score.model_router import ModelScoringRouter
from trader.recommendation.application.ports.loaded_profile import ScoringHeadRuntimeStatus
from trader.recommendation.domain.publication.models import Strategy


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
            runtime_anchor="14:50",
        )


def test_v2_router_delegates_both_head_capabilities() -> None:
    tomorrow = _ScoringCapability("v2", Strategy.TOMORROW)
    d25 = _ScoringCapability("v2", Strategy.D25)
    router = ModelScoringRouter("v2", {Strategy.TOMORROW: tomorrow, Strategy.D25: d25})

    assert router.history_required_sessions(Strategy.TOMORROW) == 61
    assert router.history_required_sessions(Strategy.D25) == 61
    assert router.uses_model(Strategy.TOMORROW) is True
    assert router.uses_model(Strategy.D25) is True
    assert router.score(Strategy.TOMORROW, ()) == "tomorrow-batch"
    assert router.score(Strategy.D25, ()) == "d25-batch"


def test_v3_router_delegates_two_independent_head_capabilities() -> None:
    capabilities = {strategy: _ScoringCapability("v3", strategy) for strategy in (Strategy.TOMORROW, Strategy.D25)}
    router = ModelScoringRouter("v3", capabilities)

    for strategy in capabilities:
        assert router.uses_model(strategy) is True
        assert router.history_required_sessions(strategy) == 61
        assert router.score(strategy, ()) == f"{strategy.value}-batch"
    status = router.status()
    assert status is not None
    assert status.profile_id == "v3"
    assert tuple(status.heads) == (Strategy.TOMORROW, Strategy.D25)
    assert all(capability.score_calls == 1 for capability in capabilities.values())
    assert all(capability.status_calls == 2 for capability in capabilities.values())


def test_router_rejects_long_as_a_non_scoring_strategy() -> None:
    router = ModelScoringRouter("v2", {Strategy.TOMORROW: _ScoringCapability("v2", Strategy.TOMORROW)})

    with pytest.raises(ValueError, match="long strategy"):
        router.uses_model(Strategy.LONG)
