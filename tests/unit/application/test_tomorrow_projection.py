from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tests.unit.application.pipeline_helpers import observed_input_stages, observed_quality_input
from tests.unit.application.review_helpers import review
from tests.unit.application.scoring_helpers import profile_for
from trader.infra.settings import LoadStrategySettings
from trader.recommendation.application.pipeline.final_selection.decision_projection import (
    BuildScoredHybrid,
    BuildScoredLocal,
    ScoredProjectionInputs,
    _model_eligible_features,
)
from trader.recommendation.application.pipeline.freeze_publish.decision_coverage import ScoredDecisionCoverage
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import UnifiedDecisionIndex
from trader.recommendation.application.pipeline.local_score.model_router import ModelScoringRouter
from trader.recommendation.application.pipeline.local_score.model_scoring import ProductionModelScoringService
from trader.recommendation.application.pipeline.quality_check.input_quality_service import QualityScoringBatch
from trader.recommendation.application.pipeline.quality_check.pipeline_status import (
    BuildSupplyStatus,
    UpdateSupplyStatusDecision,
)
from trader.recommendation.application.ports.loaded_profile import ModelInput, ModelPrediction
from trader.recommendation.application.ports.scoring import D25NativeInput, ScoredNativeInput, TomorrowNativeInput
from trader.recommendation.domain.evidence.pipeline import PipelineStage, StageState, ValidateStageBatchContinuity
from trader.recommendation.domain.market.models import Board, FeatureSnapshot
from trader.recommendation.domain.publication.decision_identity import (
    CommittedDecisionRecord,
    FormalScoredDecision,
    VisibleScoredSeats,
)
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.residualization import (
    LEGACY_EXPOSURE_CONTRACT,
    TRAINED_HEAD_EXPOSURE_CONTRACT,
)
from trader.recommendation.domain.selection.execution_cost import ExecutionCostPolicy
from trader.recommendation.domain.selection.scored_selection import ScoredCandidateStageCounts
from trader.recommendation.infra.persistence.decision_record_codec import (
    CommittedRecordBytes,
    CommittedRecordFromBytes,
)
from trader.recommendation.infra.policy_settings import _recommendation_policy
from trader.training.application.research_audit import BuildCommittedResearchAudit

SHANGHAI = ZoneInfo("Asia/Shanghai")
TRADE_DATE = date(2026, 7, 29)
EVALUATED_AT = datetime(2026, 7, 29, 14, 40, tzinfo=SHANGHAI)
PROJECT_ROOT = Path(__file__).resolve().parents[3]


class _ProductionPredictor:
    profile_id = "v2"
    model_id = "daily_reconstructible_ensemble"
    model_hash = "b" * 64
    feature_ids = (
        "qfq_return_1d",
        "qfq_return_3d",
        "qfq_return_5d",
        "qfq_residual_momentum_20d_skip5",
        "qfq_residual_momentum_40d_skip5",
        "qfq_residual_momentum_60d_skip5",
    )
    exposure_contract = LEGACY_EXPOSURE_CONTRACT
    industry_ids: tuple[str, ...] = ()

    def predict(self, inputs: tuple[ModelInput, ...]) -> tuple[ModelPrediction, ...]:
        return tuple(ModelPrediction(item.code, 0.01 + index / 1000.0) for index, item in enumerate(inputs))


class _RecordingProductionPredictor(_ProductionPredictor):
    def __init__(self) -> None:
        self.codes: tuple[str, ...] = ()

    def predict(self, inputs: tuple[ModelInput, ...]) -> tuple[ModelPrediction, ...]:
        self.codes = tuple(item.code for item in inputs)
        return super().predict(inputs)


class _NonPositiveProductionPredictor(_ProductionPredictor):
    def predict(self, inputs: tuple[ModelInput, ...]) -> tuple[ModelPrediction, ...]:
        return tuple(ModelPrediction(item.code, 0.001 + (index % 3) * 0.0001) for index, item in enumerate(inputs))


class _IndustryProductionPredictor(_ProductionPredictor):
    exposure_contract = TRAINED_HEAD_EXPOSURE_CONTRACT
    industry_ids = ("J66货币金融服务",)


def test_model_eligibility_reuses_known_boards_but_preserves_freshness_and_board_correction(
    application_feature_factory,
):
    policy = _recommendation_policy(LoadStrategySettings(PROJECT_ROOT / "config" / "strategy.json"))
    feature = _verified_feature(application_feature_factory("600000", EVALUATED_AT - timedelta(seconds=10)))
    feature = replace(feature, quote=replace(feature.quote, board=Board.MAIN))
    assert _model_eligible_features((feature,), policy, evaluated_at=EVALUATED_AT, max_age_seconds=30)[0] is feature
    unknown = replace(feature, quote=replace(feature.quote, board=Board.UNSUPPORTED))
    corrected = _model_eligible_features((unknown,), policy, evaluated_at=EVALUATED_AT, max_age_seconds=30)[0]
    assert corrected.quote.board is Board.MAIN
    assert corrected == feature
    assert not _model_eligible_features((feature,), policy, evaluated_at=EVALUATED_AT, max_age_seconds=1)


def test_quality_batch_membership_is_linear_and_checks_complete_values(application_feature_factory, monkeypatch):
    policy = _recommendation_policy(LoadStrategySettings(PROJECT_ROOT / "config" / "strategy.json"))
    features = tuple(
        _verified_feature(application_feature_factory(f"600{index:03d}", EVALUATED_AT - timedelta(seconds=10)))
        for index in range(40)
    )
    native = _native_input(features)
    _, quality = observed_quality_input(features, policy, EVALUATED_AT)
    comparisons = 0
    original = FeatureSnapshot.__eq__

    def counted_equal(first, second):
        nonlocal comparisons
        comparisons += 1
        return original(first, second)

    monkeypatch.setattr(FeatureSnapshot, "__eq__", counted_equal)
    QualityScoringBatch(quality, native)
    assert len(quality.records) == len(features)
    assert comparisons <= len(features)
    changed = replace(features[0], values={**features[0].values, "ma5": 12345.0})
    with pytest.raises(ValueError, match="must belong"):
        QualityScoringBatch(quality, replace(native, candidate_features=(changed, *features[1:])))


def test_fourteenth_stage_cannot_select_beyond_the_global_score_six(application_feature_factory) -> None:
    class LastRowPositivePredictor(_ProductionPredictor):
        def predict(self, inputs: tuple[ModelInput, ...]) -> tuple[ModelPrediction, ...]:
            return tuple(ModelPrediction(item.code, 1.0 if item.code == "600006" else -0.10) for item in inputs)

    policy = _recommendation_policy(LoadStrategySettings(PROJECT_ROOT / "config" / "strategy.json"))
    policy = replace(policy, selection=replace(policy.selection, thresholds={"tomorrow": 0.0, "d25": 0.0}))
    features = tuple(
        _with_model_features(
            _verified_feature(application_feature_factory(f"600{index:03d}", EVALUATED_AT - timedelta(seconds=10))),
            0,
        )
        for index in range(7)
    )
    native = _native_input(features)
    _, quality = observed_quality_input(features, policy, EVALUATED_AT)
    projection = BuildScoredLocal(
        native,
        policy,
        sequence=1,
        runtime=ScoredProjectionInputs(
            model_scoring=_router(LastRowPositivePredictor()),
            quality_batch=QualityScoringBatch(quality, native),
        ),
    )
    stages = projection.stages.snapshots

    assert stages[-2].output_count == 1
    assert stages[-1].input_count == 1
    assert stages[-1].output_count == 1
    assert ScoredDecisionCoverage(projection.local).selected_count == 1
    assert all(item.selected for item in VisibleScoredSeats(projection.local))


def _router(predictor: object) -> ModelScoringRouter:
    service = ProductionModelScoringService(
        profile_for(predictor),
        Strategy.TOMORROW,
        cost_policy=ExecutionCostPolicy(0.002, 1.0),
    )  # type: ignore[arg-type]
    return ModelScoringRouter("v2", {Strategy.TOMORROW: service})


def test_native_local_and_valid_facts_publish_one_parented_hybrid(
    application_feature_factory,
) -> None:
    policy = _recommendation_policy(LoadStrategySettings(PROJECT_ROOT / "config" / "strategy.json"))
    features = tuple(
        _verified_feature(application_feature_factory(f"600{index:03d}", EVALUATED_AT - timedelta(seconds=10)))
        for index in range(100)
    )
    model_features = tuple(_with_model_features(feature, index) for index, feature in enumerate(features))
    projection = BuildScoredLocal(
        _native_input(model_features),
        policy,
        sequence=1,
        runtime=ScoredProjectionInputs(model_scoring=_router(_ProductionPredictor())),
    )
    assert projection.review_candidates
    assert all(item.name.startswith("测试") for item in projection.local.items)
    assert all(item.industry == "工业" for item in projection.local.items)
    assert all(item.quote is not None for item in projection.local.items)
    quote = projection.local.items[0].quote
    assert quote is not None
    source_quote = next(
        feature.quote for feature in model_features if feature.quote.code == projection.local.items[0].code
    )
    assert quote.price == source_quote.price
    assert quote.pct_change == source_quote.pct_change
    assert quote.amount == source_quote.amount
    assert quote.turnover_rate == source_quote.turnover_rate
    assert quote.market_cap == source_quote.market_cap
    assert quote.source == source_quote.source
    assert quote.source_time == source_quote.source_time
    assert quote.data_version == source_quote.data_version
    assert projection.local.selection_diagnostics is not None
    assert projection.local.selection_diagnostics.executable_threshold == 78.0
    assert projection.local.selection_diagnostics.observation_floor == 72.0
    assert projection.input_quality.history_required_sessions == 61
    assert ("score_scale", "weighted_evidence_quality_0_100") in projection.local.input_versions
    assert ("score_model", f"daily_reconstructible_ensemble:{'b' * 64}") in projection.local.input_versions
    assert projection.cost_reference_version is not None
    assert ("cost_reference", projection.cost_reference_version) in projection.local.input_versions
    assert any(name.startswith("board_population:") for name, _version in projection.local.input_versions)
    assert all(item.model_diagnostics is not None for item in projection.local.items)
    assert all("model_prediction_rank" not in dict(item.score_components) for item in projection.local.items)
    assert all("tail_structure" in dict(item.score_components) for item in projection.local.items)
    assert all(
        item.model_diagnostics.predicted_net_excess_pct > 0 for item in projection.local.items if item.model_diagnostics
    )
    assert all(item.setup_type is not None for item in projection.local.items)
    assert all(item.downside is not None for item in projection.local.items)
    assert all(item.research_coverage is not None for item in projection.local.items)
    audit = BuildCommittedResearchAudit(projection, projection.local)
    assert audit.decision_hash == projection.local.content_hash
    assert audit.deepseek_request_delta == 0
    assert audit.shadow_mode == "control_copy"
    assert audit.production_local == audit.research_shadow
    assert audit.passed_candidates
    assert all(candidate.board and candidate.industry for candidate in audit.passed_candidates)
    assert any(candidate.full_population_scored for candidate in audit.passed_candidates)
    code = projection.review_candidates[0].code
    applied = replace(review(code, 100.0), completed_at=EVALUATED_AT + timedelta(seconds=5))
    hybrid = BuildScoredHybrid(
        projection,
        policy,
        {code: applied},
        review_deadline=EVALUATED_AT.replace(hour=14, minute=48),
    )

    assert hybrid is not None
    assert hybrid.decision is not None
    hybrid = hybrid.decision
    assert hybrid.parent_version == projection.local.version
    assert hybrid.input_versions == projection.local.input_versions
    local_scores = {item.code: item.final_score for item in projection.local.items}
    assert all(item.final_score == local_scores[item.code] for item in hybrid.items)
    record = CommittedDecisionRecord(FormalScoredDecision(hybrid), EVALUATED_AT.replace(hour=15, minute=0), "scheduled")
    encoded = CommittedRecordBytes(record)
    restored = CommittedRecordFromBytes(encoded)
    assert restored == record
    assert CommittedRecordBytes(restored) == encoded
    assert restored.decision.input_versions == projection.local.input_versions
    reviewed = next(item for item in hybrid.items if item.code == code)
    assert reviewed.review_outcome == "applied"
    index = UnifiedDecisionIndex()
    local_result = index.publish(projection.local, expected_version=None)
    hybrid_result = index.publish(hybrid, expected_version=projection.local.version)
    assert local_result.accepted and hybrid_result.accepted
    assert hybrid_result.event is not None
    assert hybrid_result.event.decision_version == hybrid.version
    assert index.snapshot(Strategy.TOMORROW).current == hybrid


def test_tomorrow_non_positive_utility_keeps_scores_but_cannot_enter_recommendation_pools(
    application_feature_factory,
) -> None:
    policy = _recommendation_policy(LoadStrategySettings(PROJECT_ROOT / "config" / "strategy.json"))
    features = tuple(
        _with_model_features(
            _verified_feature(application_feature_factory(f"600{index:03d}", EVALUATED_AT - timedelta(seconds=10))),
            index,
        )
        for index in range(1, 4)
    )

    native = _native_input(features)
    input_stages, quality_stage = observed_quality_input(features, policy, EVALUATED_AT)
    projection = BuildScoredLocal(
        native,
        policy,
        sequence=1,
        runtime=ScoredProjectionInputs(
            model_scoring=_router(_NonPositiveProductionPredictor()),
            quality_batch=QualityScoringBatch(quality_stage, native),
        ),
    )

    diagnostics = projection.local.selection_diagnostics
    assert diagnostics is not None
    assert diagnostics.maximum_final_score is None
    assert diagnostics.evaluated_count == 0
    assert diagnostics.empty_reason == "no_positive_net_utility"
    assert projection.review_candidates == ()
    assert projection.local.items == ()
    assert len(projection.model_diagnostics) == 3
    assert projection.stages.model_filter.snapshot.rejected_count == 3
    complete_status = BuildSupplyStatus(projection, input_stages=input_stages)
    assert complete_status.primary_blocker == "model_net_utility_non_positive"
    assert all(complete_status.stage_snapshots[index] is stage for index, stage in enumerate(input_stages))
    assert tuple(stage.stage for stage in complete_status.stage_snapshots) == tuple(PipelineStage)
    ValidateStageBatchContinuity(complete_status.stage_snapshots)
    updated = UpdateSupplyStatusDecision(complete_status, projection, projection.local, scored_stages=projection.stages)
    assert all(updated.stage_snapshots[index] is stage for index, stage in enumerate(input_stages))
    assert complete_status.stage_snapshots[-1].stage is PipelineStage.ACTION_PUBLISH
    assert complete_status.stage_snapshots[-1].state is StageState.READY
    assert complete_status.stage_snapshots[-1].input_count == 0
    assert complete_status.stage_snapshots[-1].output_count == 0
    assert projection.stages.action_publish.snapshot.state is StageState.READY
    assert complete_status.pipeline.stage("local_data").state == "completed"
    assert complete_status.pipeline.stage("market_features").state == "completed"
    degraded_status = BuildSupplyStatus(projection, candidate_quote_eligible=0, input_stages=input_stages)
    assert degraded_status.pipeline == complete_status.pipeline
    assert degraded_status.pipeline.stage("market_features").output_count == 3


def test_tomorrow_model_cross_section_excludes_hard_filter_rejections(
    application_feature_factory,
) -> None:
    policy = _recommendation_policy(LoadStrategySettings(PROJECT_ROOT / "config" / "strategy.json"))
    accepted = _with_model_features(
        _verified_feature(application_feature_factory("600001", EVALUATED_AT - timedelta(seconds=10))),
        1,
    )
    rejected = _with_model_features(
        _verified_feature(application_feature_factory("600002", EVALUATED_AT - timedelta(seconds=10))),
        2,
    )
    rejected = replace(rejected, quote=replace(rejected.quote, is_st=True))
    predictor = _RecordingProductionPredictor()

    projection = BuildScoredLocal(
        _native_input((accepted, rejected)),
        policy,
        sequence=1,
        runtime=ScoredProjectionInputs(model_scoring=_router(predictor)),
    )

    assert predictor.codes == ("600001",)
    rejected_evaluation = next(item for item in projection.selection.evaluations if item.code == "600002")
    assert rejected_evaluation.disposition.value == "reject"


def test_cost_reference_uses_full_model_eligible_board_before_candidate_limits(application_feature_factory) -> None:
    policy = _recommendation_policy(LoadStrategySettings(PROJECT_ROOT / "config" / "strategy.json"))
    features = tuple(
        _with_model_features(
            _verified_feature(application_feature_factory(f"600{index:03d}", EVALUATED_AT - timedelta(seconds=10))),
            index,
        )
        for index in range(123)
    )
    requested = features[30]
    # Model-qualified peers remain in the reference even with no local scoring fields.
    population = tuple(
        replace(
            feature,
            values={
                name: value
                for name, value in feature.values.items()
                if name.startswith("qfq_") or name == "amount_median_20d"
            },
        )
        for feature in features[:121]
    )
    rejected = replace(features[121], quote=replace(features[121].quote, is_st=True))
    pending = replace(
        features[122], values={name: value for name, value in features[122].values.items() if name != "qfq_return_1d"}
    )
    native = replace(_native_input((requested,)), market_features=(*population, rejected, pending))
    projection = BuildScoredLocal(
        native, policy, sequence=1, runtime=ScoredProjectionInputs(model_scoring=_router(_ProductionPredictor()))
    )
    diagnostics = dict(projection.model_diagnostics)[requested.quote.code]
    assert diagnostics.estimated_cost_pct == pytest.approx(0.25)
    assert projection.cost_reference_version is not None
    assert ("cost_reference", projection.cost_reference_version) in projection.local.input_versions


def test_supply_status_identifies_the_model_input_stage_as_the_first_blocker(
    application_feature_factory,
) -> None:
    policy = _recommendation_policy(LoadStrategySettings(PROJECT_ROOT / "config" / "strategy.json"))
    feature = _with_model_features(
        _verified_feature(application_feature_factory("600001", EVALUATED_AT - timedelta(seconds=10))),
        1,
    )
    stage_counts = ScoredCandidateStageCounts(
        issuer_eligible_population=1,
        input_ready_population=1,
        dynamic_filter_eligible=1,
        strategy_history_eligible=1,
        model_input_eligible=0,
        scoring_input_eligible=0,
        scoring_input_count=0,
    )
    native = _native_input((feature,))
    input_stages, quality_stage = observed_quality_input((feature,), policy, EVALUATED_AT)
    projection = BuildScoredLocal(
        native,
        policy,
        sequence=1,
        runtime=ScoredProjectionInputs(
            model_scoring=_router(_IndustryProductionPredictor()),
            candidate_stage_counts=stage_counts,
            quality_batch=QualityScoringBatch(quality_stage, native),
        ),
    )

    status = BuildSupplyStatus(projection, stage_counts, input_stages=input_stages)

    assert status.pipeline.stage("market_features").output_count == 1
    assert status.pipeline.stage("model_filter").output_count == 0
    assert status.primary_blocker == "production_model_features_missing"


def test_tomorrow_model_excludes_only_candidate_below_its_61_session_requirement(
    application_feature_factory,
) -> None:
    policy = _recommendation_policy(LoadStrategySettings(PROJECT_ROOT / "config" / "strategy.json"))
    eligible = _with_model_features(
        _verified_feature(application_feature_factory("600001", EVALUATED_AT - timedelta(seconds=10))),
        1,
    )
    insufficient = replace(
        _with_model_features(
            _verified_feature(application_feature_factory("600002", EVALUATED_AT - timedelta(seconds=10))),
            2,
        ),
        history_days=60,
    )
    predictor = _RecordingProductionPredictor()

    projection = BuildScoredLocal(
        _native_input((eligible, insufficient)),
        policy,
        sequence=1,
        runtime=ScoredProjectionInputs(model_scoring=_router(predictor)),
    )

    assert predictor.codes == ("600001",)
    assert projection.input_quality.history_required_sessions == 61
    assert projection.input_quality.history_covered_count == 1
    assert projection.input_quality.history_coverage_ratio == 0.5
    assert projection.input_quality.candidate_scored_count == 1
    skipped = next(item for item in projection.selection.evaluations if item.code == "600002")
    assert skipped.selection_skip_reason == "strategy_history_insufficient"


def test_tomorrow_model_history_coverage_requires_the_loaded_profile_fields(
    application_feature_factory,
) -> None:
    policy = _recommendation_policy(LoadStrategySettings(PROJECT_ROOT / "config" / "strategy.json"))
    eligible = _with_model_features(
        _verified_feature(application_feature_factory("600001", EVALUATED_AT - timedelta(seconds=10))),
        1,
    )
    missing_values = dict(
        _with_model_features(
            _verified_feature(application_feature_factory("600002", EVALUATED_AT - timedelta(seconds=10))),
            2,
        ).values
    )
    missing_values["qfq_momentum_60d_skip5"] = None
    incomplete = replace(
        _with_model_features(
            _verified_feature(application_feature_factory("600002", EVALUATED_AT - timedelta(seconds=10))),
            2,
        ),
        values=missing_values,
    )

    projection = BuildScoredLocal(
        _native_input((eligible, incomplete)),
        policy,
        sequence=1,
        runtime=ScoredProjectionInputs(model_scoring=_router(_ProductionPredictor())),
    )

    assert projection.input_quality.history_required_sessions == 61
    assert projection.input_quality.history_covered_count == 1
    assert projection.input_quality.history_coverage_ratio == 0.5
    assert projection.input_quality.candidate_scored_count == 1
    skipped = next(item for item in projection.selection.evaluations if item.code == "600002")
    assert skipped.selection_skip_reason == "production_model_features_missing"


@pytest.mark.parametrize("native_type", (TomorrowNativeInput, D25NativeInput))
@pytest.mark.parametrize(
    ("offset", "expected"),
    (
        (-6.01, "unavailable"),
        (-6.0, "observe"),
        (-5.99, "observe"),
        (-0.01, "observe"),
        (0.0, "executable"),
        (0.01, "executable"),
    ),
)
def test_configured_action_floors_reach_decision_projection(
    application_feature_factory,
    native_type: type[ScoredNativeInput],
    offset: float,
    expected: str,
) -> None:
    policy = _recommendation_policy(LoadStrategySettings(PROJECT_ROOT / "config" / "strategy.json"))
    strategy = native_type.strategy
    threshold = policy.selection.thresholds[strategy.value]
    score = round(threshold + offset, 2)
    weights = {
        board: {key: float(key == "entry_quality") for key in policy.board_local_strategy_weights[strategy][board]}
        for board in (Board.MAIN, Board.CHINEXT, Board.STAR)
    }
    policy = replace(policy, board_local_strategy_weights={**policy.board_local_strategy_weights, strategy: weights})
    features = tuple(
        _verified_feature(application_feature_factory(f"600{index:03d}", EVALUATED_AT - timedelta(seconds=10)))
        for index in range(100)
    )
    features = tuple(
        replace(
            feature,
            values={**feature.values, "entry_quality": score, "volume_to_5d_average": 0.6, "max_drawdown_20d": -2.0},
        )
        for feature in features
    )
    native = replace(_native_input(features, native_type), phase="close_fallback")

    projection = BuildScoredLocal(native, policy, sequence=1)

    diagnostics = projection.local.selection_diagnostics
    assert diagnostics is not None
    assert diagnostics.executable_threshold == threshold
    assert diagnostics.observation_floor == threshold - 6.0
    item = projection.local.items[0]
    assert item.final_score == item.local_score == score
    assert item.action.value == expected, item.reason


@pytest.mark.parametrize("native_type", (TomorrowNativeInput, D25NativeInput))
def test_close_fallback_never_consumes_a_model_head_or_its_61_day_requirement(
    application_feature_factory,
    native_type: type[ScoredNativeInput],
) -> None:
    policy = _recommendation_policy(LoadStrategySettings(PROJECT_ROOT / "config" / "strategy.json"))
    feature = _verified_feature(application_feature_factory("600001", EVALUATED_AT - timedelta(seconds=10)))
    predictor = _RecordingProductionPredictor()
    native_input = _native_input((feature,), native_type)
    native_input = replace(native_input, phase="close_fallback")

    projection = BuildScoredLocal(
        native_input,
        policy,
        sequence=1,
        runtime=ScoredProjectionInputs(model_scoring=_router(predictor)),
    )

    assert predictor.codes == ()
    assert projection.input_quality.history_required_sessions == 20
    assert projection.input_quality.candidate_scored_count == 1


def test_d25_native_local_and_valid_facts_publish_one_parented_hybrid(
    application_feature_factory,
) -> None:
    policy = _recommendation_policy(LoadStrategySettings(PROJECT_ROOT / "config" / "strategy.json"))
    features = tuple(
        _verified_feature(application_feature_factory(f"600{index:03d}", EVALUATED_AT - timedelta(seconds=10)))
        for index in range(100)
    )
    projection = BuildScoredLocal(_native_input(features, D25NativeInput), policy, sequence=1)
    assert projection.review_candidates
    code = projection.review_candidates[0].code
    applied = replace(review(code, 100.0), completed_at=EVALUATED_AT + timedelta(seconds=5))
    hybrid = BuildScoredHybrid(
        projection,
        policy,
        {code: applied},
        review_deadline=EVALUATED_AT.replace(hour=14, minute=48),
    )

    assert hybrid is not None
    assert hybrid.decision is not None
    hybrid = hybrid.decision
    assert hybrid.parent_version == projection.local.version
    index = UnifiedDecisionIndex()
    local_result = index.publish(projection.local, expected_version=None)
    hybrid_result = index.publish(hybrid, expected_version=projection.local.version)
    assert local_result.accepted and hybrid_result.accepted
    assert hybrid_result.event is not None
    assert hybrid_result.event.decision_version == hybrid.version
    assert index.snapshot(Strategy.D25).current == hybrid


@pytest.mark.parametrize("native_type", (TomorrowNativeInput, D25NativeInput))
def test_native_inputs_reuse_already_normalized_immutable_features(application_feature_factory, native_type):
    feature = _verified_feature(application_feature_factory("600000", EVALUATED_AT - timedelta(seconds=10)))
    native = native_type(
        TRADE_DATE,
        "afternoon",
        "data",
        "config",
        EVALUATED_AT,
        (feature,),
        (feature.quote.code,),
        (feature,),
        30.0,
        30.0,
    )
    assert native.market_features[0] is feature
    assert native.candidate_features[0] is feature
    invalid = replace(feature, observed_at=feature.observed_at.replace(tzinfo=None))
    with pytest.raises(ValueError, match="timezone-aware"):
        replace(native, candidate_features=(invalid,))


@pytest.mark.parametrize("native_type", (TomorrowNativeInput, D25NativeInput))
def test_native_projection_scores_fresh_candidates_against_their_coherent_older_market_batch(
    application_feature_factory,
    native_type: type[ScoredNativeInput],
) -> None:
    policy = _recommendation_policy(LoadStrategySettings(PROJECT_ROOT / "config" / "strategy.json"))
    market_at = EVALUATED_AT - timedelta(seconds=90)
    candidate_at = EVALUATED_AT - timedelta(seconds=5)
    market_features = tuple(
        _verified_feature(application_feature_factory(f"600{index:03d}", market_at)) for index in range(100)
    )
    market_features = tuple(
        replace(
            feature,
            quote=replace(
                feature.quote,
                source_time=feature.quote.source_time.astimezone(timezone.utc),
                received_time=feature.quote.received_time.astimezone(timezone.utc),
            ),
            observed_at=feature.observed_at.astimezone(timezone.utc),
        )
        for feature in market_features
    )
    candidate_features = tuple(
        _verified_feature(application_feature_factory(f"600{index:03d}", candidate_at)) for index in range(100)
    )
    native_input = native_type(
        trade_date=TRADE_DATE,
        phase="final_review",
        data_version="candidate-data:delayed-enrichment",
        config_version="runtime-current+strategy-fixture",
        evaluated_at=EVALUATED_AT,
        market_features=market_features,
        requested_codes=tuple(feature.quote.code for feature in candidate_features),
        candidate_features=candidate_features,
        preselect_max_age_seconds=30.0,
        score_max_age_seconds=30.0,
    )

    projection = BuildScoredLocal(native_input, policy, sequence=1)

    assert all(
        getattr(feature.observed_at.tzinfo, "key", None) == "Asia/Shanghai" for feature in native_input.market_features
    )
    assert projection.selection.population_rejected_count == 0
    assert projection.input_quality.candidate_scored_count == 100
    assert ("score_scale", "weighted_evidence_quality_0_100") in projection.local.input_versions
    assert "stale_quote" not in projection.selection.population_filter_reason_counts
    assert all(item.local_score is not None for item in projection.selection.scored_candidates)


def test_native_projection_does_not_treat_stale_candidate_quotes_as_fresh_population_quotes(
    application_feature_factory,
) -> None:
    policy = _recommendation_policy(LoadStrategySettings(PROJECT_ROOT / "config" / "strategy.json"))
    market_at = EVALUATED_AT - timedelta(seconds=90)
    market_features = tuple(
        _verified_feature(application_feature_factory(f"600{index:03d}", market_at)) for index in range(100)
    )
    native_input = TomorrowNativeInput(
        trade_date=TRADE_DATE,
        phase="final_review",
        data_version="candidate-data:stale",
        config_version="runtime-current+strategy-fixture",
        evaluated_at=EVALUATED_AT,
        market_features=market_features,
        requested_codes=tuple(feature.quote.code for feature in market_features),
        candidate_features=market_features,
        preselect_max_age_seconds=30.0,
        score_max_age_seconds=30.0,
    )

    projection = BuildScoredLocal(native_input, policy, sequence=1)

    assert projection.input_quality.candidate_scored_count == 0
    assert projection.input_quality.candidate_transient_reason_counts["stale_quote"] == 100


@pytest.mark.parametrize("strategy", (TomorrowNativeInput, D25NativeInput))
@pytest.mark.parametrize(
    ("field", "value", "reason"),
    (
        ("price", None, "invalid_price"),
        ("amount", None, "invalid_amount"),
        ("high", -1.0, "invalid_quote_structure"),
        ("pct_change", None, "invalid_pct_change"),
    ),
)
def test_native_projection_classifies_an_invalid_candidate_quote_as_transient(
    application_feature_factory,
    strategy,
    field,
    value,
    reason,
) -> None:
    policy = _recommendation_policy(LoadStrategySettings(PROJECT_ROOT / "config" / "strategy.json"))
    source = _verified_feature(application_feature_factory("600001", EVALUATED_AT - timedelta(seconds=10)))
    invalid = replace(source, quote=replace(source.quote, **{field: value}))

    projection = BuildScoredLocal(_native_input((invalid,), strategy), policy, sequence=1)

    assert projection.input_quality.candidate_scored_count == 0
    assert projection.input_quality.status == "transient_invalid_empty"
    assert not projection.input_quality.publishable
    assert projection.input_quality.candidate_rejected_count == 0
    assert projection.input_quality.data_pending_count == 1
    assert projection.input_quality.candidate_transient_reason_counts[reason] == 1
    stage = observed_input_stages((invalid,), policy, EVALUATED_AT)[3]
    assert stage.rejected_count == 0
    assert stage.pending_count == 1
    assert stage.input_count == stage.output_count + stage.rejected_count + stage.pending_count + stage.failed_count
    assert projection.review_candidates == ()


@pytest.mark.parametrize("strategy", (TomorrowNativeInput, D25NativeInput))
@pytest.mark.parametrize("separate_invalid", (False, True))
def test_business_rejection_takes_precedence_over_the_same_stocks_missing_input(
    application_feature_factory,
    strategy,
    separate_invalid,
) -> None:
    policy = _recommendation_policy(LoadStrategySettings(PROJECT_ROOT / "config" / "strategy.json"))
    source = _verified_feature(application_feature_factory("600001", EVALUATED_AT - timedelta(seconds=10)))
    suspended = replace(source, quote=replace(source.quote, is_suspended=True, price=None))
    other = replace(source, quote=replace(source.quote, code="600002", price=None))
    features = (suspended, other) if separate_invalid else (suspended,)

    projection = BuildScoredLocal(_native_input(features, strategy), policy, sequence=1)

    quality = projection.input_quality
    assert quality.candidate_rejected_count == 1
    assert quality.data_pending_count == int(separate_invalid)
    assert quality.refresh_pending_count == 0
    assert quality.publishable is not separate_invalid
    assert quality.status == ("transient_invalid_empty" if separate_invalid else "business_empty")
    stage = observed_input_stages(features, policy, EVALUATED_AT)[3]
    assert stage.rejected_count == 1
    assert stage.pending_count == int(separate_invalid)
    assert stage.input_count == stage.output_count + stage.rejected_count + stage.pending_count + stage.failed_count


@pytest.mark.parametrize("strategy", (TomorrowNativeInput, D25NativeInput))
def test_stale_and_invalid_input_has_one_refresh_pending_terminal_state(application_feature_factory, strategy) -> None:
    policy = _recommendation_policy(LoadStrategySettings(PROJECT_ROOT / "config" / "strategy.json"))
    source = _verified_feature(application_feature_factory("600001", EVALUATED_AT - timedelta(minutes=1)))
    invalid = replace(source, quote=replace(source.quote, price=None))

    projection = BuildScoredLocal(_native_input((invalid,), strategy), policy, sequence=1)

    assert projection.input_quality.status == "transient_invalid_empty"
    assert projection.input_quality.data_pending_count == 0
    assert projection.input_quality.refresh_pending_count == 1
    assert projection.input_quality.candidate_rejected_count == 0
    assert projection.selection.evaluations[0].selection_skip_reason == "refresh_pending"


def test_supply_status_rejects_selection_without_immutable_stage_facts(
    application_feature_factory,
) -> None:
    policy = _recommendation_policy(LoadStrategySettings(PROJECT_ROOT / "config" / "strategy.json"))
    source = _verified_feature(application_feature_factory("600001", EVALUATED_AT - timedelta(seconds=10)))
    projection = BuildScoredLocal(_native_input((source,)), policy, sequence=1)
    projection = replace(projection, selection=replace(projection.selection, stage_counts=None))

    with pytest.raises(ValueError, match="immutable candidate stage facts"):
        BuildSupplyStatus(projection)


def test_review_completed_after_1448_cannot_create_hybrid(application_feature_factory) -> None:
    policy = _recommendation_policy(LoadStrategySettings(PROJECT_ROOT / "config" / "strategy.json"))
    features = tuple(
        _verified_feature(application_feature_factory(f"600{index:03d}", EVALUATED_AT - timedelta(seconds=10)))
        for index in range(100)
    )
    projection = BuildScoredLocal(_native_input(features), policy, sequence=1)
    code = projection.review_candidates[0].code
    deadline = EVALUATED_AT.replace(hour=14, minute=48)
    late = replace(review(code, 100.0), completed_at=deadline + timedelta(microseconds=1))

    result = BuildScoredHybrid(
        projection,
        policy,
        {code: late},
        review_deadline=deadline,
    )
    assert result is not None
    assert result.decision is None
    assert any(reason.code == "deepseek_late" for reason in result.stages.risk_review.snapshot.reasons)


def test_review_completed_at_1448_cannot_create_hybrid(application_feature_factory) -> None:
    policy = _recommendation_policy(LoadStrategySettings(PROJECT_ROOT / "config" / "strategy.json"))
    features = tuple(
        _verified_feature(application_feature_factory(f"600{index:03d}", EVALUATED_AT - timedelta(seconds=10)))
        for index in range(100)
    )
    projection = BuildScoredLocal(_native_input(features), policy, sequence=1)
    code = projection.review_candidates[0].code
    deadline = EVALUATED_AT.replace(hour=14, minute=48)
    boundary_result = replace(review(code, 100.0), completed_at=deadline)

    result = BuildScoredHybrid(
        projection,
        policy,
        {code: boundary_result},
        review_deadline=deadline,
    )
    assert result is not None
    assert result.decision is None


def _native_input(
    features: tuple[FeatureSnapshot, ...],
    strategy: type[ScoredNativeInput] = TomorrowNativeInput,
) -> ScoredNativeInput:
    return strategy(
        trade_date=TRADE_DATE,
        phase="final_review",
        data_version="candidate-data:fixture",
        config_version="runtime-current+strategy-fixture",
        evaluated_at=EVALUATED_AT,
        market_features=features,
        requested_codes=tuple(feature.quote.code for feature in features),
        candidate_features=features,
        preselect_max_age_seconds=30.0,
        score_max_age_seconds=30.0,
    )


def _verified_feature(feature: FeatureSnapshot) -> FeatureSnapshot:
    return replace(
        feature,
        quote=replace(
            feature.quote,
            cross_source_verified=True,
            cross_source_deviation_pct=0.1,
        ),
    )


def _with_model_features(feature: FeatureSnapshot, index: int) -> FeatureSnapshot:
    values = dict(feature.values)
    offset = index / 1000.0
    values.update(
        {
            "qfq_return_1d": 0.01 + offset,
            "qfq_return_3d": 0.02 + offset,
            "qfq_return_5d": 0.03 + offset,
            "qfq_momentum_20d_skip5": 0.04 + offset,
            "qfq_momentum_40d_skip5": 0.05 + offset,
            "qfq_momentum_60d_skip5": 0.06 + offset,
            "qfq_amihud_20d": 0.001 + offset,
            "qfq_average_amount_20d": 100_000_000.0 + index,
        }
    )
    return replace(feature, values=values, history_days=61)
