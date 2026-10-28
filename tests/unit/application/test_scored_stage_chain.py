from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from itertools import count

import pytest

from tests.unit.application.cycle_fixture import build_cycle_fixture
from tests.unit.application.review_helpers import review
from tests.unit.application.scoring_helpers import profile_for
from tests.unit.application.test_input_runtime import (
    _freeze_build,
    _Market,
    _policy,
    _prime_scoring_cache,
    _request,
)
from tests.unit.application.test_tomorrow_projection import (
    EVALUATED_AT,
    _native_input,
    _NonPositiveProductionPredictor,
    _ProductionPredictor,
    _RecordingProductionPredictor,
    _router,
    _verified_feature,
    _with_model_features,
)
from trader.recommendation.application.pipeline.final_selection.freeze_projection import (
    BuildScoredHybrid,
    BuildScoredLocal,
    ScoredProjectionInputs,
)
from trader.recommendation.application.pipeline.freeze_publish.runtime_adapters import DeepSeekAdapter
from trader.recommendation.application.pipeline.local_score.model_router import ModelScoringRouter
from trader.recommendation.application.pipeline.local_score.model_scoring import ProductionModelScoringService
from trader.recommendation.application.ports.runtime import PipelineTaskRequest, ReviewUnavailableError
from trader.recommendation.application.ports.scoring import D25NativeInput, TomorrowNativeInput
from trader.recommendation.application.runtime.cadence import PipelineTask
from trader.recommendation.application.runtime.trading_session import TradingSession
from trader.recommendation.domain.evidence.pipeline import (
    SourceHealthState,
    StageState,
    ValidateStageBatchContinuity,
)
from trader.recommendation.domain.evidence.review import ReviewOutcome, RiskFact, RiskRule
from trader.recommendation.domain.market.models import Board, Evidence
from trader.recommendation.domain.publication.freeze_identity import FormalScoredFreeze, VisibleScoredSeats
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.publication.preview import PreviewContext
from trader.recommendation.domain.selection.execution_cost import ExecutionCostPolicy


@pytest.mark.parametrize("native_type", (TomorrowNativeInput, D25NativeInput))
def test_full_population_places_former_121st_input_third_and_veto_never_refills(
    application_feature_factory, native_type
):
    policy = _policy()
    strategy = native_type.strategy
    weights = {
        board: {name: float(name == "entry_quality") for name in policy.board_local_strategy_weights[strategy][board]}
        for board in (Board.MAIN, Board.CHINEXT, Board.STAR)
    }
    policy = replace(
        policy,
        board_local_strategy_weights={**policy.board_local_strategy_weights, strategy: weights},
        risk_rules={
            "regulatory_risk": RiskRule(
                "regulatory_risk",
                "high",
                0.0,
                0.7,
                "deepseek",
                evidence_ttl_hours=24,
                veto=True,
                allowed_evidence_types=("regulatory_filing",),
                local_trigger_enabled=False,
            )
        },
    )
    evidence = Evidence(
        "official-risk",
        "regulatory_filing",
        "官方风险公告",
        "exchange",
        EVALUATED_AT - timedelta(hours=1),
        received_at=EVALUATED_AT - timedelta(minutes=30),
        data_version="research:full",
    )
    features = []
    scores = {"600000": 81.0, "600001": 80.0, "600120": 79.0, "600002": 78.0, "300000": 77.0, "688000": 76.0}
    for prefix, board in (("600", Board.MAIN), ("300", Board.CHINEXT), ("688", Board.STAR)):
        for index in range(130):
            code = f"{prefix}{index:03d}"
            feature = _verified_feature(application_feature_factory(code, EVALUATED_AT - timedelta(seconds=10)))
            features.append(
                replace(
                    feature,
                    quote=replace(feature.quote, board=board, industry="同一行业"),
                    values={**feature.values, "entry_quality": scores.get(code, 10.0)},
                    evidence=(evidence,),
                )
            )
    projection = BuildScoredLocal(_native_input(tuple(reversed(features)), native_type), policy, sequence=1)
    assert len(projection.selection.scored_candidates) == 390
    assert projection.stages.local_risk.snapshot.output_count == 390
    assert projection.stages.fixed_selection.snapshot.not_selected_count == 384
    expected = ("600000", "600001", "600120", "600002", "300000", "688000")
    assert tuple(item.code for item in VisibleScoredSeats(projection.local)) == expected
    assert tuple(item.local_score for item in VisibleScoredSeats(projection.local)) == (81, 80, 79, 78, 77, 76)
    assert "600120" in {item.code for item in projection.review_candidates}
    fact = RiskFact(
        "risk:official",
        "regulatory_risk",
        "high",
        0.0,
        "deepseek",
        EVALUATED_AT,
        confidence=1.0,
        evidence_ids=("official-risk",),
        veto=True,
    )
    reviewed = replace(review("600120", 100), completed_at=EVALUATED_AT, risk_facts=(fact,))
    hybrid = BuildScoredHybrid(
        projection, policy, {"600120": reviewed}, review_deadline=EVALUATED_AT + timedelta(minutes=1)
    )
    assert hybrid is not None and hybrid.freeze is not None
    assert hybrid.stages.fixed_selection is projection.stages.fixed_selection
    assert tuple(item.code for item in VisibleScoredSeats(hybrid.freeze)) == expected
    assert tuple(item.code for item in VisibleScoredSeats(FormalScoredFreeze(hybrid.freeze))) == expected
    vetoed = next(item for item in hybrid.freeze.items if item.code == "600120")
    assert vetoed.action.value == "unavailable" and vetoed.selected and vetoed.rank == 3
    assert vetoed.final_score == vetoed.local_score == 79.0
    review_counts = {reason.code: reason.count for reason in hybrid.stages.risk_review.snapshot.reasons}
    assert review_counts["deepseek_completed"] == 1
    assert review_counts.get("deepseek_source_failed", 0) == len(projection.review_candidates) - 1
    for stage in hybrid.stages.snapshots:
        assert stage.input_count == (
            stage.output_count
            + stage.rejected_count
            + stage.pending_count
            + stage.failed_count
            + stage.not_selected_count
        )


def _features(factory):
    return tuple(
        _with_model_features(_verified_feature(factory(f"600{index:03d}", EVALUATED_AT - timedelta(seconds=10))), index)
        for index in range(100)
    )


@pytest.mark.parametrize("native_type", (TomorrowNativeInput, D25NativeInput))
def test_real_scored_handoffs_measure_operations_and_match_selected_freeze(application_feature_factory, native_type):
    features = _features(application_feature_factory)
    before = tuple(features)
    ticks = count()
    projection = BuildScoredLocal(
        _native_input(features, native_type),
        _policy(),
        sequence=1,
        runtime=ScoredProjectionInputs(monotonic=lambda: float(next(ticks))),
    )
    stages = projection.stages
    assert features == before
    assert stages.local_risk.records == projection.selection.scored_candidates
    assert tuple(item.code for item in stages.action_publish.records) == tuple(
        item.code for item in sorted(projection.local.items, key=lambda item: item.rank) if item.selected
    )
    assert all(snapshot.latency_ms == 1000 for snapshot in stages.snapshots)
    assert all(snapshot.rejected_count == 0 for snapshot in stages.snapshots)
    assert all(
        output.business_rejections is None
        for output in (
            stages.model_filter,
            stages.local_score,
            stages.local_risk,
            stages.fixed_selection,
            stages.risk_review,
            stages.action_publish,
        )
    )
    assert all(
        left.output_batch_id == right.input_batch_id and left.output_count == right.input_count
        for left, right in zip(stages.snapshots, stages.snapshots[1:], strict=False)
    )
    assert all(
        item.score.local_score
        == next(row.local_score for row in stages.fixed_selection.records if row.code == item.code)
        for item in stages.action_publish.records
    )


@pytest.mark.parametrize("preview", (False, True))
def test_hybrid_reuses_local_scoring_and_adds_vendor_elapsed_only_to_review(application_feature_factory, preview):
    router = _router(_ProductionPredictor())
    native = _native_input(_features(application_feature_factory))
    evaluated_at = EVALUATED_AT.replace(hour=15, minute=0, second=10) if preview else EVALUATED_AT
    context = PreviewContext("projection-preview", evaluated_at.date(), evaluated_at) if preview else None
    if context is not None:
        features = tuple(
            replace(
                feature,
                quote=replace(feature.quote, source_time=context.market_as_of, received_time=evaluated_at),
                observed_at=evaluated_at,
            )
            for feature in native.market_features
        )
        native = replace(native, market_features=features, candidate_features=features, evaluated_at=evaluated_at)
    native = replace(native, preview_context=context)
    local = BuildScoredLocal(
        native,
        _policy(),
        sequence=1,
        runtime=ScoredProjectionInputs(model_scoring=router),
    )
    assert local.review_candidates
    ticks = count()
    result = BuildScoredHybrid(
        local,
        _policy(),
        {
            item.code: replace(review(item.code, 100), completed_at=evaluated_at + timedelta(seconds=5))
            for item in local.review_candidates
        },
        review_deadline=evaluated_at + timedelta(minutes=8),
        monotonic=lambda: float(next(ticks)),
        review_latency_ms=73,
    )
    assert result is not None and result.freeze is not None
    assert local.local.preview_context is result.freeze.preview_context is context
    assert result.stages.local_score is local.stages.local_score
    assert result.stages.risk_review.snapshot.latency_ms == 1073
    assert result.stages.fixed_selection is local.stages.fixed_selection
    assert result.stages.action_publish.snapshot.latency_ms == 1000
    assert result.stages.risk_review.snapshot.output_batch_id != local.stages.risk_review.snapshot.output_batch_id
    assert tuple(item.code for item in result.stages.action_publish.records) == tuple(
        item.code for item in sorted(result.freeze.items, key=lambda item: item.rank) if item.selected
    )
    assert result.freeze.parent_version == local.local.version
    assert local.stages.risk_review.records[0].review is None


@pytest.mark.parametrize("strategy", (Strategy.TOMORROW, Strategy.D25))
def test_non_trading_day_real_adapters_predict_and_register_review_after_15(application_feature_factory, strategy):
    observed_at = EVALUATED_AT.replace(month=10, day=24, hour=16, minute=0)
    context = PreviewContext(f"adapter-preview-{strategy.value}", observed_at.date() - timedelta(days=1), observed_at)
    policy = _policy()
    strong_local_values = {
        name: 95.0 for component in policy.local_component_weights[strategy].values() for name in component
    }
    features = []
    for index in range(300):
        prefix, board = (("600", Board.MAIN), ("300", Board.CHINEXT), ("688", Board.STAR))[index % 3]
        feature = _with_model_features(
            _verified_feature(
                application_feature_factory(f"{prefix}{index:03d}", context.market_as_of, industry=f"行业{index % 4}")
            ),
            index,
        )
        features.append(
            replace(
                feature,
                quote=replace(
                    feature.quote,
                    board=board,
                    board_source="security_master",
                    board_reliability="verified",
                    received_time=observed_at,
                ),
                values={**feature.values, **strong_local_values, "entry_quality": 95.0},
                observed_at=observed_at,
                preview_context=context,
            )
        )
    predictor = _RecordingProductionPredictor()
    router = _router(predictor)
    if strategy is Strategy.D25:
        router = ModelScoringRouter(
            "v2",
            {
                strategy: ProductionModelScoringService(
                    profile_for(predictor, strategy), strategy, cost_policy=ExecutionCostPolicy(0.002, 1.0)
                )
            },
        )
    clock = [observed_at]
    adapter = build_cycle_fixture(
        _Market(tuple(features)),
        config_version="preview-adapter-fixture",
        freeze_build=replace(_freeze_build(now=lambda: clock[0]), model_scoring=router),
    )
    request = replace(
        _request(observed_at, strategy=strategy, phase="before_close"),
        preview_context=context,
        allow_review=True,
        review_deadline=observed_at + timedelta(seconds=30),
    )
    _prime_scoring_cache(adapter, observed_at)
    adapter.data.refresh(request)
    local = adapter.freezes.build_local(request)
    assert local is not None and local.preview_context is context
    assert local.trade_date == observed_at.date() and local.observed_at == observed_at
    assert predictor.codes and set(predictor.codes).issubset({feature.quote.code for feature in features})
    projection = adapter.freezes.projection(local.version)
    assert projection is not None and projection.native_input.phase == "before_close"
    assert len(VisibleScoredSeats(local)) == 6
    assert all(item.model_diagnostics is not None for item in local.items)
    assert all(item.model_diagnostics.estimated_cost_pct > 0 for item in local.items if item.model_diagnostics)
    assert all(item.quote is not None and item.quote.source_time == context.market_as_of for item in local.items)
    assert projection.review_candidates
    clock[0] = observed_at + timedelta(seconds=5)
    result = BuildScoredHybrid(
        projection,
        _policy(),
        {item.code: replace(review(item.code, 100), completed_at=clock[0]) for item in projection.review_candidates},
        review_deadline=request.review_deadline,
    )
    assert result is not None and result.freeze is not None
    accepted = adapter.freezes.register_review(projection, result)
    assert accepted is not None and accepted.preview_context is context
    assert accepted.stage == "hybrid" and accepted.parent_version == local.version
    assert tuple(item.code for item in VisibleScoredSeats(accepted)) == tuple(
        item.code for item in VisibleScoredSeats(local)
    )
    assert all(item.final_score == item.local_score for item in accepted.items)
    clock[0] = request.review_deadline
    assert adapter.freezes.register_review(projection, result) is None


def test_non_trading_day_real_adapter_publishes_empty_seats_when_population_is_ineligible(application_feature_factory):
    observed_at = EVALUATED_AT.replace(month=10, day=24, hour=16, minute=0)
    context = PreviewContext("empty-adapter-preview", observed_at.date() - timedelta(days=1), observed_at)
    feature = _with_model_features(_verified_feature(application_feature_factory("600001", context.market_as_of)), 0)
    feature = replace(
        feature,
        quote=replace(feature.quote, is_st=True, received_time=observed_at),
        observed_at=observed_at,
        preview_context=context,
    )
    predictor = _RecordingProductionPredictor()
    adapter = build_cycle_fixture(
        _Market((feature,)),
        config_version="preview-empty-fixture",
        freeze_build=replace(_freeze_build(now=lambda: observed_at), model_scoring=_router(predictor)),
    )
    request = replace(
        _request(observed_at, phase="before_close"),
        preview_context=context,
        allow_review=True,
        review_deadline=observed_at + timedelta(seconds=30),
    )
    _prime_scoring_cache(adapter, observed_at)
    tail = adapter.data.refresh_task(
        PipelineTaskRequest(PipelineTask.INTRADAY_TAIL, observed_at, preview_context=context)
    )
    assert tail.data_version == "intraday:empty"
    adapter.data.refresh(request)
    local = adapter.freezes.build_local(request)
    assert local is not None and local.preview_context is context
    assert not VisibleScoredSeats(local) and not local.items
    assert not predictor.codes


@pytest.mark.parametrize(
    ("task", "code"),
    (
        (PipelineTask.FULL_MARKET, "coherent_market_epoch_unavailable"),
        (PipelineTask.CANDIDATE_QUOTES, "candidate_universe_unavailable"),
    ),
)
def test_non_trading_day_real_adapter_fails_empty_population_without_pending_retry(task, code) -> None:
    from trader.recommendation.application.ports.runtime import DataRefreshPendingError, DataRefreshUnavailableError

    observed_at = EVALUATED_AT.replace(month=10, day=24, hour=16, minute=0)
    context = PreviewContext("missing-population-preview", observed_at.date() - timedelta(days=1), observed_at)
    adapter = build_cycle_fixture(
        _Market(()), config_version="preview-missing-fixture", freeze_build=_freeze_build(now=lambda: observed_at)
    )
    with pytest.raises(DataRefreshUnavailableError, match=code) as error:
        adapter.data.refresh_task(PipelineTaskRequest(task, observed_at, preview_context=context))
    assert not isinstance(error.value, DataRefreshPendingError)


@pytest.mark.parametrize("failure", ("rejected", "empty", "late", "deadline"))
def test_unusable_reviews_leave_real_degraded_observation_without_new_freeze(application_feature_factory, failure):
    local = BuildScoredLocal(_native_input(_features(application_feature_factory)), _policy(), sequence=1)
    assert local.review_candidates
    deadline = EVALUATED_AT + timedelta(minutes=8)
    reviews = (
        {}
        if failure == "empty"
        else {
            item.code: replace(
                review(item.code, 100),
                outcome=ReviewOutcome.REJECTED if failure == "rejected" else ReviewOutcome.APPLIED,
                completed_at=EVALUATED_AT if failure == "rejected" else deadline + timedelta(seconds=failure == "late"),
            )
            for item in local.review_candidates
        }
    )
    result = BuildScoredHybrid(local, _policy(), reviews, review_deadline=deadline, review_latency_ms=73)
    assert result is not None and result.freeze is None
    snapshot = result.stages.risk_review.snapshot
    assert snapshot.state is StageState.DEGRADED
    assert snapshot.source_health.state is SourceHealthState.DEGRADED
    assert snapshot.source_health.healthy_source_count == 0
    expected = (
        "deepseek_incomplete"
        if failure == "empty"
        else "deepseek_rejected"
        if failure == "rejected"
        else "deepseek_late"
    )
    assert expected in {reason.code for reason in snapshot.reasons}
    assert snapshot.output_count == local.stages.fixed_selection.snapshot.output_count
    assert snapshot.rejected_count == snapshot.failed_count == 0
    assert snapshot.latency_ms >= 73
    assert all(not item.score.fusion_applied for item in result.stages.action_publish.records)
    assert tuple(item.score.final_score for item in result.stages.entries) == tuple(
        item.score.final_score for item in local.stages.entries
    )


def test_non_positive_model_utility_skips_local_scoring_and_leaves_seats_empty(application_feature_factory):
    local = BuildScoredLocal(
        _native_input(_features(application_feature_factory)),
        _policy(),
        sequence=1,
        runtime=ScoredProjectionInputs(model_scoring=_router(_NonPositiveProductionPredictor())),
    )
    assert local.stages.local_score.records == ()
    assert local.stages.fixed_selection.records == local.stages.action_publish.records == ()
    assert not any(item.selected for item in local.local.items)
    assert "model_net_utility_non_positive" in {reason.code for reason in local.stages.model_filter.snapshot.reasons}
    assert local.stages.model_filter.snapshot.rejected_count == 100
    assert all(snapshot.rejected_count == 0 for snapshot in local.stages.snapshots[1:])


class _FailedReviewer:
    def __init__(self, failure):
        self.failure = failure

    def review(self, strategy, features, **kwargs):
        if self.failure == "exception":
            raise OSError("fixture unavailable")
        completed_at = EVALUATED_AT.replace(tzinfo=None) if self.failure == "time" else EVALUATED_AT
        return {
            item.quote.code: replace(
                review(item.quote.code, 100), completed_at=completed_at, evidence_manifest_hash="manifest:fixture"
            )
            for item in features
        }

    def evidence_manifest_hash(self, feature):
        if self.failure == "manifest_exception":
            raise ValueError("fixture manifest validation failed")
        return "manifest:fixture" if self.failure == "time" else "manifest:mismatch"


@pytest.mark.parametrize(
    ("failure", "after_deadline"),
    (
        ("exception", False),
        ("manifest", False),
        ("manifest_exception", False),
        ("time", False),
        ("exception", True),
        ("manifest", True),
    ),
)
def test_review_adapter_records_failure_and_cannot_overwrite_new_input_observations(
    application_feature_factory, failure, after_deadline
):
    adapter = build_cycle_fixture(
        _Market(_features(application_feature_factory)),
        config_version="fixture",
        freeze_build=_freeze_build(now=lambda: EVALUATED_AT),
    )
    request = _request(EVALUATED_AT, phase="afternoon")
    _prime_scoring_cache(adapter, EVALUATED_AT)
    adapter.data.refresh(request)
    local = adapter.freezes.build_local(request)
    assert local is not None
    initial_status = next(item for item in adapter.data.input_quality_status() if item.strategy is Strategy.TOMORROW)
    assert local.pipeline is not None
    assert local.pipeline.stage_snapshots == initial_status.stage_snapshots
    ValidateStageBatchContinuity(local.pipeline.stage_snapshots)
    projection = adapter.freezes.projection(local.version)
    assert projection is not None and projection.review_candidates
    failed_at = request.review_deadline + timedelta(microseconds=1) if after_deadline else EVALUATED_AT
    reviewer = DeepSeekAdapter(_FailedReviewer(failure), _policy(), adapter.freezes, now=lambda: failed_at)
    if failure in {"exception", "manifest_exception"}:
        with pytest.raises(ReviewUnavailableError) as error:
            reviewer.build_hybrid(local, request)
        assert isinstance(error.value.__cause__, OSError if failure == "exception" else ValueError)
    else:
        assert reviewer.build_hybrid(local, request) is None
    status = next(item for item in adapter.data.input_quality_status() if item.strategy is Strategy.TOMORROW)
    ValidateStageBatchContinuity(status.stage_snapshots)
    expected = {
        "exception": "deepseek_review_unavailable",
        "manifest": "deepseek_manifest_mismatch",
        "manifest_exception": "deepseek_manifest_validation_failed",
        "time": "deepseek_review_time_invalid",
    }[failure]
    assert expected in {
        reason.code
        for reason in next(stage for stage in status.stage_snapshots if stage.stage.value == "risk_review").reasons
    }
    if after_deadline:
        assert "deepseek_late" in {
            reason.code
            for reason in next(stage for stage in status.stage_snapshots if stage.stage.value == "risk_review").reasons
        }
    assert status.summary.highest_final_score == max(item.final_score for item in local.items)
    assert adapter.freezes.projection(local.version) is projection

    observed = BuildScoredHybrid(projection, _policy(), {}, review_deadline=request.review_deadline)
    assert observed is not None
    later = EVALUATED_AT + timedelta(seconds=1)
    adapter.data.refresh_task(PipelineTaskRequest(PipelineTask.FULL_MARKET, later))
    pending = adapter.data.input_quality_status()
    assert adapter.freezes.register_review(projection, observed) is None
    assert adapter.data.input_quality_status() == pending


@pytest.mark.parametrize("successful", (True, False))
@pytest.mark.parametrize(
    "superseded_by", ("none", "input", "local", "date", "resume", "close", "cas_close", "cas_input")
)
def test_review_writes_require_current_parent_input_session_and_open_day(
    application_feature_factory, monkeypatch, successful, superseded_by
):
    clock = [EVALUATED_AT]
    adapter = build_cycle_fixture(
        _Market(_features(application_feature_factory)),
        config_version="fixture",
        freeze_build=_freeze_build(now=lambda: clock[0]),
    )
    request = _request(EVALUATED_AT, phase="afternoon")
    if superseded_by in {"date", "resume"}:
        session = TradingSession(lambda: clock[0], lambda: clock[0].timestamp())
        request = replace(request, session=session.capture())
    _prime_scoring_cache(adapter, EVALUATED_AT)
    adapter.data.refresh(request)
    local = adapter.freezes.build_local(request)
    projection = adapter.freezes.projection(local.version)
    assert projection is not None and projection.review_candidates
    result = BuildScoredHybrid(
        projection,
        _policy(),
        {item.code: replace(review(item.code, 100), completed_at=EVALUATED_AT) for item in projection.review_candidates}
        if successful
        else {},
        review_deadline=request.review_deadline,
    )
    assert result is not None
    assert (result.freeze is not None) == successful
    if superseded_by == "input":
        adapter.data.refresh_task(PipelineTaskRequest(PipelineTask.FULL_MARKET, EVALUATED_AT + timedelta(seconds=1)))
    elif superseded_by == "local":
        newer = replace(request, sequence=2, input_version="newer-local")
        adapter.data.refresh(newer)
        assert adapter.freezes.build_local(newer).version != local.version
    elif superseded_by == "date":
        clock[0] += timedelta(days=1)
    elif superseded_by == "resume":
        clock[0] += timedelta(minutes=6)
    elif superseded_by == "close":
        clock[0] = clock[0].replace(hour=15, minute=0)
    elif superseded_by in {"cas_close", "cas_input"}:
        from trader.recommendation.application.pipeline.local_score import local_freeze_builder

        update = local_freeze_builder.UpdateSupplyStatusFreeze

        def advance_before_commit(*args, **kwargs):
            quality = update(*args, **kwargs)
            if superseded_by == "cas_close":
                clock[0] = clock[0].replace(hour=15, minute=0)
            else:
                adapter.data.refresh_task(
                    PipelineTaskRequest(PipelineTask.FULL_MARKET, EVALUATED_AT + timedelta(seconds=1))
                )
            return quality

        monkeypatch.setattr(local_freeze_builder, "UpdateSupplyStatusFreeze", advance_before_commit)
    before = adapter.data.input_quality_status()
    accepted = adapter.freezes.register_review(projection, result)
    after = adapter.data.input_quality_status()
    if superseded_by != "none":
        assert accepted is None
        assert after == before
    else:
        assert after != before
        updated = next(item for item in after if item.strategy is Strategy.TOMORROW)
        ValidateStageBatchContinuity(updated.stage_snapshots)
        if successful:
            assert accepted is not None and accepted.pipeline.stage_snapshots == updated.stage_snapshots
            assert {item.code: item.final_score for item in accepted.items} == {
                item.code: item.final_score for item in local.items
            }
        else:
            assert accepted is None
            assert updated.stage_snapshots[10].state is StageState.DEGRADED
    assert adapter.freezes.projection(local.version) is projection


def test_local_diagnostics_cannot_commit_after_inputs_advance_during_scoring(application_feature_factory, monkeypatch):
    from trader.recommendation.application.pipeline.local_score import local_freeze_builder

    adapter = build_cycle_fixture(
        _Market(_features(application_feature_factory)),
        config_version="fixture",
        freeze_build=_freeze_build(now=lambda: EVALUATED_AT),
    )
    request = _request(EVALUATED_AT, phase="afternoon")
    _prime_scoring_cache(adapter, EVALUATED_AT)
    adapter.data.refresh(request)
    before = adapter.data.input_quality_status()
    build = local_freeze_builder.BuildSupplyStatus

    def advance_before_commit(*args, **kwargs):
        quality = build(*args, **kwargs)
        adapter.data.refresh_task(PipelineTaskRequest(PipelineTask.FULL_MARKET, EVALUATED_AT + timedelta(seconds=1)))
        return quality

    monkeypatch.setattr(local_freeze_builder, "BuildSupplyStatus", advance_before_commit)
    local = adapter.freezes.build_local(request)
    assert local is not None
    assert adapter.data.input_quality_status() == before
    projection = adapter.freezes.projection(local.version)
    observed = BuildScoredHybrid(projection, _policy(), {}, review_deadline=request.review_deadline)
    assert observed is not None
    assert adapter.freezes.register_review(projection, observed) is None
    assert adapter.data.input_quality_status() == before
