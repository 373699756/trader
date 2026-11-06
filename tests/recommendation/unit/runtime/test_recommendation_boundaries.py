from __future__ import annotations

from dataclasses import replace

from datetime import datetime, timedelta


import pytest

from tests.helpers.shared.module_boundaries import LayerImportViolations, SourceImports, SourceModuleName

from tests.helpers.recommendation.cycle_fixture import CycleFixtureDependencies, build_cycle_fixture

from tests.helpers.recommendation.review_helpers import review

from tests.helpers.recommendation.runtime.input_runtime import _freeze_build, _Market, _prime_scoring_cache, _request

from tests.helpers.recommendation.publication.tomorrow_projection import EVALUATED_AT, _native_input

from tests.helpers.recommendation.selection.application.tomorrow_selection import _data_snapshot

from tests.helpers.recommendation.selection.application.tomorrow_selection import _policy as snapshot_policy

from trader.infra.atomic_files.json import AtomicReadJson, AtomicWriteJson

from trader.infra.clock.shanghai import ShanghaiClock


from trader.recommendation.application.pipeline.candidate_pool.candidate_pool_service import CandidateFilteringService

from trader.recommendation.application.pipeline.dynamic_filter.filter_executor import ScoredSelectionUseCase


from trader.recommendation.application.pipeline.final_selection.grouped_ranking import RankingSelectionService

from trader.recommendation.application.pipeline.freeze_publish.draft_index import UnifiedFreezeDraftIndex

from trader.recommendation.application.pipeline.local_score.base_scoring import LocalScoringService

from trader.recommendation.application.pipeline.local_score.model_capability import PublishedModelScoringService


from trader.recommendation.application.pipeline.score_merge.score_fusion import ScoreFusionService


from trader.recommendation.application.ports.runtime import FreezeUnavailableError



from trader.recommendation.domain.publication.models import Strategy


from tests.helpers.recommendation.runtime.recommendation_boundaries import (
    SHANGHAI,
    SOURCE_ROOT,
    _Reader,
    _RecordingCandidateFiltering,
    _RecordingFeatureCalculation,
    _RecordingLocalScoring,
    _RecordingModelCapability,
    _RecordingRankingSelection,
    _RecordingRiskControl,
    _features,
    _runtime_policy,
)


def test_market_adapter_uses_candidate_and_local_boundaries(application_feature_factory) -> None:
    observed_at = datetime(2026, 8, 12, 14, 40, tzinfo=SHANGHAI)
    feature = application_feature_factory("600001", observed_at - timedelta(seconds=1))
    policy = _runtime_policy()
    candidate = _RecordingCandidateFiltering(CandidateFilteringService(policy, None))
    local = _RecordingLocalScoring(LocalScoringService())
    adapter = build_cycle_fixture(
        _Market((feature,)),
        config_version="test-config",
        freeze_build=CycleFixtureDependencies(
            _freeze_build().long_runtime,
            policy,
            UnifiedFreezeDraftIndex(),
            lambda: observed_at,
            candidate_filtering=candidate,
            local_scoring=local,
        ),
    )
    request = _request(observed_at, phase="afternoon")

    _prime_scoring_cache(adapter, observed_at)
    adapter.data.refresh(request)
    try:
        adapter.freezes.build_local(request)
    except FreezeUnavailableError:
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
    assert fused.freeze is not None
    assert risk.calls == len(scored.local.items)
    assert all(item.setup_type == "fixture" for item in fused.freeze.items)


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
    assert fused.freeze is not None
    assert fused.freeze.parent_version == projection.local.version


def test_recommendation_application_does_not_import_infrastructure_or_training() -> None:
    violations = tuple(
        f"{path.relative_to(SOURCE_ROOT)}:{imported.line} -> {imported.targets[-1]}"
        for path in sorted((SOURCE_ROOT / "recommendation/application").rglob("*.py"))
        for imported in LayerImportViolations(SourceModuleName(path, SOURCE_ROOT), SourceImports(path, SOURCE_ROOT))
    )
    assert violations == ()


def test_atomic_json_has_one_infrastructure_owner() -> None:
    owner = SOURCE_ROOT / "infra/atomic_files/json.py"
    retired = SOURCE_ROOT / "infra/persistence/runtime_json.py"
    assert owner.is_file()
    assert not retired.exists()
    assert "AtomicWriteJson" in owner.read_text(encoding="utf-8")


def test_atomic_json_round_trip_and_shanghai_clock_boundary(tmp_path) -> None:
    path = tmp_path / "runtime" / "state.json"
    AtomicWriteJson(path, {"ready": True, "count": 2})

    assert AtomicReadJson(path) == {"count": 2, "ready": True}
    aware = datetime(2026, 9, 15, 7, 10, tzinfo=SHANGHAI)
    assert ShanghaiClock(lambda: aware).now() == aware

    with pytest.raises(ValueError, match="timezone-aware"):
        ShanghaiClock(lambda: datetime(2026, 9, 15, 7, 10)).now()
