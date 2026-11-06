"""Verify real scoring paths with fixture predictors, without claiming native-model timing."""

from __future__ import annotations
from dataclasses import replace
from tests.helpers.recommendation.runtime.performance_fixtures import _budgets, _history_sessions, _profile
from trader.recommendation.infra.settings.loading import LoadStrategySettings
from trader.recommendation.application.ports.loaded_profile import ModelPrediction
from trader.recommendation.domain.selection.execution_cost import ExecutionCostPolicy
from trader.recommendation.infra.performance.fixtures import BuildPerformanceFixtures
from trader.recommendation.infra.performance.model_workload import PerformanceProfileWorkload
from trader.recommendation.infra.policy_settings import _recommendation_policy


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
    policy = _recommendation_policy(LoadStrategySettings(Path("config/strategy.json"), scoring_profile=profile_id))
    return PerformanceProfileWorkload(profile, policy, features, "fixture-config", ExecutionCostPolicy(0.002, 1.0))
