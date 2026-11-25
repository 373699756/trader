"""Verify real scoring paths with fixture predictors, without claiming native-model timing."""

from __future__ import annotations
from dataclasses import replace
from tests.helpers.recommendation.runtime.performance_fixtures import _budgets, _history_sessions, _profile
from trader.recommendation.infra.settings.loading import LoadStrategySettings
from trader.recommendation.application.ports.loaded_profile import ModelInput, ModelPrediction
from trader.recommendation.domain.selection.execution_cost import ExecutionCostPolicy
from trader.recommendation.infra.performance.fixtures import BuildPerformanceFixtures
from trader.recommendation.infra.performance.model_workload import PerformanceProfileWorkload
from trader.recommendation.infra.policy_settings import BuildRecommendationPolicy


class _Predictor:
    def __init__(self, base):
        self.profile_id = base.profile_id
        self.feature_ids = base.feature_ids
        self.industry_ids = base.industry_ids
        self.model_id = "learned_rank" if base.profile_id == "v1" else base.model_id
        self.model_hash = base.model_hash
        self.exposure_contract = base.exposure_contract

    def predict(self, inputs):
        if self.profile_id == "v5":
            assert all(item.sequence is not None for item in inputs)
        return tuple(
            ModelPrediction(
                item.code,
                0.02 + item.alpha_features[1] * 0.0001,
                item.alpha_features[0] if self.profile_id == "v1" else None,
            )
            for item in inputs
        )


class _D25FixturePredictor(_Predictor):
    def predict(self, inputs: tuple[ModelInput, ...]) -> tuple[ModelPrediction, ...]:
        return tuple(replace(item, target_probability=0.75) for item in super().predict(inputs))


class _TomorrowFixturePredictor:
    def __init__(self, profile: str) -> None:
        self.identity = f"{int(profile[1:]):064x}"

    def predict_probability(self, features: tuple[float, ...]) -> float:
        return 0.75

    def predict_probabilities(self, rows: tuple[tuple[float, ...], ...]) -> tuple[float, ...]:
        return tuple(self.predict_probability(row) for row in rows)


def _install_performance_models(monkeypatch) -> None:
    from trader.entrypoints import performance
    from trader.recommendation.domain.publication.models import Strategy

    def bind_d25(_root, profile, target):
        assert target.strategy is Strategy.D25
        loaded = _profile(profile)
        head = loaded.heads[Strategy.D25]
        return replace(loaded, heads={Strategy.D25: replace(head, predictor=_D25FixturePredictor(head.predictor))})

    monkeypatch.setattr(performance, "LoadProductionD25Profile", bind_d25)
    monkeypatch.setattr(
        "trader.training.infra.tomorrow.artifact.LoadTomorrowProbabilityModel",
        lambda _root, profile, _target, **_settings: _TomorrowFixturePredictor(profile),
    )


def _workload(profile_id):
    from pathlib import Path

    profile = _profile(profile_id)
    profile = replace(
        profile,
        heads={
            strategy: replace(head, predictor=_Predictor(head.predictor)) for strategy, head in profile.heads.items()
        },
    )
    _, _, features = BuildPerformanceFixtures(
        _budgets(), profile, history_sessions=_history_sessions(profile_id)
    )
    policy = BuildRecommendationPolicy(LoadStrategySettings(Path("config/strategy.json"), scoring_profile=profile_id))
    return PerformanceProfileWorkload(profile, policy, features, "fixture-config", ExecutionCostPolicy(0.002, 1.0))
