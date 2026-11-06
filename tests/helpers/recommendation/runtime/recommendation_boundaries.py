from __future__ import annotations
from datetime import timedelta
from pathlib import Path
from tests.helpers.recommendation.publication.tomorrow_projection import EVALUATED_AT, _verified_feature
from trader.infra.settings import LoadStrategySettings
from trader.recommendation.application.pipeline.candidate_pool.candidate_pool_service import (
    CandidateFilteringPort,
    CandidateFilteringService,
)
from trader.recommendation.application.pipeline.dynamic_filter.filter_executor import AssembleScoredFeatures
from trader.recommendation.application.pipeline.dynamic_standardize.dynamic_feature_builder import (
    FeatureCalculationPort,
)
from trader.recommendation.application.pipeline.final_selection.grouped_ranking import (
    RankingSelectionPort,
    RankingSelectionService,
)
from trader.recommendation.application.pipeline.local_score.base_scoring import LocalScoringPort, LocalScoringService
from trader.recommendation.application.pipeline.policy import RecommendationPolicy
from trader.recommendation.application.ports.loaded_profile import ModelScoringContext
from trader.recommendation.domain.market.data_plane import MarketDataPlaneSnapshot
from trader.recommendation.domain.market.models import FeatureSnapshot
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.infra.policy_settings import _recommendation_policy


def _features(factory) -> tuple[FeatureSnapshot, ...]:
    return tuple(
        _verified_feature(factory(f"{prefix}{index:03d}", EVALUATED_AT - timedelta(seconds=10)))
        for prefix in ("600", "300", "688")
        for index in range(100)
    )


PROJECT_ROOT = Path(__file__).resolve().parents[4]

SOURCE_ROOT = PROJECT_ROOT / "src" / "trader"

SHANGHAI = EVALUATED_AT.tzinfo


class _RecordingCandidateFiltering(CandidateFilteringPort):
    def __init__(self, delegate: CandidateFilteringService) -> None:
        self._delegate = delegate
        self.plan_calls = 0
        self.refresh_calls = 0

    def plan(self, *args, **kwargs):
        self.plan_calls += 1
        return self._delegate.plan(*args, **kwargs)

    def plan_observed(self, *args, **kwargs):
        self.plan_calls += 1
        return self._delegate.plan_observed(*args, **kwargs)

    def refresh(self, *args, **kwargs):
        self.refresh_calls += 1
        return self._delegate.refresh(*args, **kwargs)


class _RecordingLocalScoring(LocalScoringPort):
    def __init__(self, delegate: LocalScoringService) -> None:
        self._delegate = delegate
        self.calls = 0

    def score(self, *args, **kwargs):
        self.calls += 1
        return self._delegate.score(*args, **kwargs)


class _RecordingFeatureCalculation(FeatureCalculationPort):
    def __init__(self) -> None:
        self.calls = 0

    def calculate(self, snapshot: MarketDataPlaneSnapshot) -> tuple[FeatureSnapshot, ...]:
        self.calls += 1
        return AssembleScoredFeatures(snapshot)


class _Reader:
    def __init__(self, snapshot: MarketDataPlaneSnapshot) -> None:
        self._snapshot = snapshot

    def snapshot(self) -> MarketDataPlaneSnapshot:
        return self._snapshot


class _RecordingRiskControl:
    def __init__(self) -> None:
        self.calls = 0

    def assess(self, features: FeatureSnapshot, strategy: Strategy):
        from trader.recommendation.domain.risk.downside import DownsideAssessment

        del features, strategy
        self.calls += 1
        return DownsideAssessment("pass", (), 1.0, 0.0, -1.0, "fixture")


class _RecordingRankingSelection(RankingSelectionPort):
    def __init__(self, delegate: RankingSelectionService) -> None:
        self._delegate = delegate
        self.calls = 0

    def select(self, *args, **kwargs):
        self.calls += 1
        return self._delegate.select(*args, **kwargs)


class _RecordingModelCapability:
    def __init__(self) -> None:
        self.score_calls = 0
        self._status = object()

    def uses_model(self, strategy: Strategy) -> bool:
        del strategy
        return True

    def history_required_sessions(self, strategy: Strategy) -> int:
        del strategy
        return 61

    def is_input_eligible(self, strategy: Strategy, feature: FeatureSnapshot) -> bool:
        del strategy, feature
        return True

    def score(self, strategy, features, *, context: ModelScoringContext | None = None):
        del strategy, features, context
        self.score_calls += 1
        return "published-batch"

    def status(self):
        return self._status


def _runtime_policy() -> RecommendationPolicy:
    return _recommendation_policy(LoadStrategySettings(PROJECT_ROOT / "config" / "strategy.json"))
