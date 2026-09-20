from __future__ import annotations

import ast
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tests.unit.application.review_helpers import review
from tests.unit.application.test_input_runtime import _decision_build, _Market, _prime_scoring_cache, _request
from tests.unit.application.test_tomorrow_projection import EVALUATED_AT, _native_input, _verified_feature
from tests.unit.application.test_tomorrow_selection import _data_snapshot
from tests.unit.application.test_tomorrow_selection import _policy as snapshot_policy
from trader.recommendation.application.pipeline.freeze_publish.draft_index import UnifiedDecisionDraftIndex
from trader.recommendation.application.pipeline.data_source.source_router import DecisionBuildDependencies, MarketDataAdapter
from trader.recommendation.domain.market.data_plane import MarketDataPlaneSnapshot
from trader.recommendation.application.ports.loaded_profile import ModelScoringContext
from trader.recommendation.application.ports.runtime import DecisionUnavailableError
from trader.recommendation.application.pipeline.candidate_pool.candidate_pool_service import (
    CandidateFilteringPort,
    CandidateFilteringService,
)
from trader.recommendation.application.pipeline.dynamic_standardize.dynamic_feature_builder import FeatureCalculationPort
from trader.recommendation.application.pipeline.local_score.base_scoring import LocalScoringPort, LocalScoringService
from trader.recommendation.application.pipeline.local_score.model_capability import PublishedModelScoringService
from trader.recommendation.application.pipeline.policy import RecommendationPolicy
from trader.recommendation.application.pipeline.final_selection.grouped_ranking import RankingSelectionPort, RankingSelectionService
from trader.recommendation.application.pipeline.score_merge.score_fusion import ScoreFusionService
from trader.recommendation.application.pipeline.dynamic_filter.filter_executor import (
    ScoredSelectionUseCase,
    assemble_scored_features,
)
from trader.bootstrap import _recommendation_policy
from trader.recommendation.domain.market.models import FeatureSnapshot
from trader.recommendation.domain.publication.models import Strategy
from trader.infra.atomic_files.json import atomic_read_json, atomic_write_json
from trader.infra.clock.shanghai import ShanghaiClock
from trader.infra.settings import load_strategy_settings


def _features(factory) -> tuple[FeatureSnapshot, ...]:
    return tuple(
        _verified_feature(factory(f"{prefix}{index:03d}", EVALUATED_AT - timedelta(seconds=10)))
        for prefix in ("600", "300", "688")
        for index in range(100)
    )

PROJECT_ROOT = Path(__file__).resolve().parents[3]
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
        return assemble_scored_features(snapshot)


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
    return _recommendation_policy(load_strategy_settings(PROJECT_ROOT / "config" / "strategy.json"))


def test_market_adapter_uses_candidate_and_local_boundaries(application_feature_factory) -> None:
    observed_at = datetime(2026, 8, 12, 14, 40, tzinfo=SHANGHAI)
    feature = application_feature_factory("600001", observed_at - timedelta(seconds=1))
    policy = _runtime_policy()
    candidate = _RecordingCandidateFiltering(CandidateFilteringService(policy, None, 1))
    local = _RecordingLocalScoring(LocalScoringService())
    adapter = MarketDataAdapter(
        _Market((feature,)),
        config_version="test-config",
        candidate_pool_size=1,
        decision_build=DecisionBuildDependencies(
            _decision_build().long_runtime,
            policy,
            UnifiedDecisionDraftIndex(),
            lambda: observed_at,
            candidate_filtering=candidate,
            local_scoring=local,
        ),
    )
    request = _request(observed_at, phase="afternoon")

    _prime_scoring_cache(adapter, observed_at)
    adapter.refresh(request)
    try:
        adapter.build_local(request)
    except DecisionUnavailableError:
        # The fixture is intentionally minimal; the boundary call is the contract under test.
        pass

    assert candidate.plan_calls == 1
    assert candidate.refresh_calls == 1
    assert local.calls == 1


def test_selection_use_case_delegates_feature_assembly() -> None:
    snapshot = _data_snapshot()
    calculator = _RecordingFeatureCalculation()
    use_case = ScoredSelectionUseCase(_Reader(snapshot), snapshot_policy(_runtime_policy()), calculator)

    use_case.execute(evaluated_at=snapshot.market.observed_at, max_age_seconds=60.0)  # type: ignore[union-attr]

    assert calculator.calls == 1


def test_local_scoring_injects_risk_control(application_feature_factory) -> None:
    risk = _RecordingRiskControl()
    policy = _runtime_policy()
    scored = LocalScoringService(risk_control=risk).score(
        _native_input(_features(application_feature_factory)),
        policy,
        sequence=1,
    )

    assert scored.local.items
    assert risk.calls == len(scored.local.items)
    assert all(item.setup_type == "fixture" for item in scored.local.items)

    code = scored.review_candidates[0].code
    candidate_review = replace(review(code, 100.0), completed_at=EVALUATED_AT + timedelta(seconds=5))
    fused = ScoreFusionService().fuse(
        scored,
        policy,
        {code: candidate_review},
        review_deadline=EVALUATED_AT.replace(hour=14, minute=50, second=0),
    )

    assert fused is not None
    assert risk.calls == len(scored.local.items)
    assert all(item.setup_type == "fixture" for item in fused.items)


def test_local_scoring_injects_ranking_selection(application_feature_factory) -> None:
    ranking = _RecordingRankingSelection(RankingSelectionService())
    policy = _runtime_policy()
    scored = LocalScoringService(ranking_selection=ranking).score(
        _native_input(_features(application_feature_factory)),
        policy,
        sequence=1,
    )

    assert scored.local.items
    assert ranking.calls == 1


def test_published_model_scoring_only_delegates_to_a_capability() -> None:
    capability = _RecordingModelCapability()
    service = PublishedModelScoringService(capability)  # type: ignore[arg-type]

    assert service.uses_model(Strategy.TOMORROW) is True
    assert service.history_required_sessions(Strategy.TOMORROW) == 61
    assert service.is_input_eligible(Strategy.TOMORROW, object()) is True  # type: ignore[arg-type]
    assert service.score(Strategy.TOMORROW, ()) == "published-batch"
    assert service.status() is capability._status
    assert capability.score_calls == 1


def test_score_fusion_service_preserves_manifest_gate_and_parentage(application_feature_factory) -> None:
    policy = _runtime_policy()
    projection = LocalScoringService().score(_native_input(_features(application_feature_factory)), policy, sequence=1)
    code = projection.review_candidates[0].code
    deadline = EVALUATED_AT.replace(hour=14, minute=50, second=0)
    candidate_review = replace(review(code, 100.0), completed_at=EVALUATED_AT + timedelta(seconds=5))
    service = ScoreFusionService()

    assert service.manifests_match(projection, {code: candidate_review}, {code: f"manifest:{code}"})
    fused = service.fuse(projection, policy, {code: candidate_review}, review_deadline=deadline)

    assert fused is not None
    assert fused.parent_version == projection.local.version


def test_recommendation_application_does_not_import_infrastructure_or_training() -> None:
    forbidden_prefixes = ("trader.infra", "trader.web", "trader.entrypoints")
    violations: list[str] = []
    for path in (SOURCE_ROOT / "recommendation/application").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            else:
                continue
            violations.extend(
                f"{path.relative_to(SOURCE_ROOT)} -> {name}"
                for name in names
                if name.startswith(forbidden_prefixes) or ".training" in name or name.endswith(".download")
            )
    assert violations == []


def test_atomic_json_has_one_infrastructure_owner() -> None:
    owner = SOURCE_ROOT / "infra/atomic_files/json.py"
    retired = SOURCE_ROOT / "infra/persistence/runtime_json.py"
    assert owner.is_file()
    assert not retired.exists()
    assert "atomic_write_json" in owner.read_text(encoding="utf-8")


def test_atomic_json_round_trip_and_shanghai_clock_boundary(tmp_path) -> None:
    path = tmp_path / "runtime" / "state.json"
    atomic_write_json(path, {"ready": True, "count": 2})

    assert atomic_read_json(path) == {"count": 2, "ready": True}
    aware = datetime(2026, 9, 15, 7, 10, tzinfo=SHANGHAI)
    assert ShanghaiClock(lambda: aware).now() == aware

    with pytest.raises(ValueError, match="timezone-aware"):
        ShanghaiClock(lambda: datetime(2026, 9, 15, 7, 10)).now()
