from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from itertools import count

import pytest

from tests.unit.application.cycle_fixture import build_cycle_fixture
from tests.unit.application.review_helpers import review
from tests.unit.application.test_input_runtime import (
    _decision_build,
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
    _router,
    _verified_feature,
    _with_model_features,
)
from trader.recommendation.application.pipeline.final_selection.decision_projection import (
    BuildScoredHybrid,
    BuildScoredLocal,
    ScoredProjectionInputs,
)
from trader.recommendation.application.pipeline.freeze_publish.runtime_adapters import DeepSeekAdapter
from trader.recommendation.application.ports.runtime import PipelineTaskRequest, ReviewUnavailableError
from trader.recommendation.application.ports.scoring import D25NativeInput, TomorrowNativeInput
from trader.recommendation.application.runtime.cadence import PipelineTask
from trader.recommendation.application.runtime.trading_session import TradingSession
from trader.recommendation.domain.evidence.pipeline import (
    SourceHealthState,
    StageState,
    ValidateStageBatchContinuity,
)
from trader.recommendation.domain.evidence.review import ReviewOutcome
from trader.recommendation.domain.publication.models import Strategy


def _features(factory):
    return tuple(
        _with_model_features(_verified_feature(factory(f"600{index:03d}", EVALUATED_AT - timedelta(seconds=10))), index)
        for index in range(100)
    )


@pytest.mark.parametrize("native_type", (TomorrowNativeInput, D25NativeInput))
def test_real_scored_handoffs_measure_operations_and_match_selected_decision(application_feature_factory, native_type):
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
    assert stages.local_score.records == projection.selection.scored_candidates
    assert tuple(item.code for item in stages.final_selection.records) == tuple(
        item.code for item in sorted(projection.local.items, key=lambda item: item.rank) if item.selected
    )
    assert all(snapshot.latency_ms == 1000 for snapshot in stages.snapshots)
    assert all(snapshot.rejected_count == 0 for snapshot in stages.snapshots)
    assert all(
        output.business_rejections is None
        for output in (
            stages.local_score,
            stages.risk_review,
            stages.score_merge,
            stages.downside_action,
            stages.final_selection,
        )
    )
    assert all(
        left.output_batch_id == right.input_batch_id and left.output_count == right.input_count
        for left, right in zip(stages.snapshots, stages.snapshots[1:], strict=False)
    )
    assert all(item.score.local_score == item.evaluation.local_score for item in stages.score_merge.records)


def test_hybrid_reuses_local_scoring_and_adds_vendor_elapsed_only_to_review(application_feature_factory):
    router = _router(_ProductionPredictor())
    local = BuildScoredLocal(
        _native_input(_features(application_feature_factory)),
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
            item.code: replace(review(item.code, 100), completed_at=EVALUATED_AT + timedelta(seconds=5))
            for item in local.review_candidates
        },
        review_deadline=EVALUATED_AT + timedelta(minutes=8),
        monotonic=lambda: float(next(ticks)),
        review_latency_ms=73,
    )
    assert result is not None and result.decision is not None
    assert result.stages.local_score is local.stages.local_score
    assert result.stages.risk_review.snapshot.latency_ms == 1073
    assert all(snapshot.latency_ms == 1000 for snapshot in result.stages.snapshots[2:])
    assert result.stages.risk_review.snapshot.output_batch_id != local.stages.risk_review.snapshot.output_batch_id
    assert tuple(item.code for item in result.stages.final_selection.records) == tuple(
        item.code for item in sorted(result.decision.items, key=lambda item: item.rank) if item.selected
    )
    assert result.decision.parent_version == local.local.version
    assert local.stages.risk_review.records[0].review is None


@pytest.mark.parametrize("failure", ("rejected", "empty", "late", "deadline"))
def test_unusable_reviews_leave_real_degraded_observation_without_new_decision(application_feature_factory, failure):
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
    assert result is not None and result.decision is None
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
    assert snapshot.output_count == local.stages.local_score.snapshot.output_count
    assert snapshot.rejected_count == snapshot.failed_count == 0
    assert snapshot.latency_ms >= 73
    assert all(not item.score.fusion_applied for item in result.stages.score_merge.records)
    assert tuple(item.score.final_score for item in result.stages.entries) == tuple(
        item.score.final_score for item in local.stages.entries
    )


def test_non_positive_model_utility_preserves_scores_and_leaves_action_output_empty(application_feature_factory):
    local = BuildScoredLocal(
        _native_input(_features(application_feature_factory)),
        _policy(),
        sequence=1,
        runtime=ScoredProjectionInputs(model_scoring=_router(_NonPositiveProductionPredictor())),
    )
    assert local.stages.local_score.records
    assert local.stages.downside_action.records == local.stages.final_selection.records == ()
    assert not any(item.selected for item in local.local.items)
    assert "model_net_utility_non_positive" in {reason.code for reason in local.stages.downside_action.snapshot.reasons}
    assert all(snapshot.rejected_count == 0 for snapshot in local.stages.snapshots)


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
        candidate_pool_size=120,
        decision_build=_decision_build(now=lambda: EVALUATED_AT),
    )
    request = _request(EVALUATED_AT, phase="afternoon")
    _prime_scoring_cache(adapter, EVALUATED_AT)
    adapter.data.refresh(request)
    local = adapter.decisions.build_local(request)
    assert local is not None
    initial_status = next(item for item in adapter.data.input_quality_status() if item.strategy is Strategy.TOMORROW)
    assert local.pipeline is not None
    assert local.pipeline.stage_snapshots == initial_status.stage_snapshots
    ValidateStageBatchContinuity(local.pipeline.stage_snapshots)
    projection = adapter.decisions.projection(local.version)
    assert projection is not None and projection.review_candidates
    failed_at = request.review_deadline + timedelta(microseconds=1) if after_deadline else EVALUATED_AT
    reviewer = DeepSeekAdapter(_FailedReviewer(failure), _policy(), adapter.decisions, now=lambda: failed_at)
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
    assert expected in {reason.code for reason in status.stage_snapshots[10].reasons}
    if after_deadline:
        assert "deepseek_late" in {reason.code for reason in status.stage_snapshots[10].reasons}
    assert status.summary.highest_final_score == max(item.final_score for item in local.items)
    assert adapter.decisions.projection(local.version) is projection

    observed = BuildScoredHybrid(projection, _policy(), {}, review_deadline=request.review_deadline)
    assert observed is not None
    later = EVALUATED_AT + timedelta(seconds=1)
    adapter.data.refresh_task(PipelineTaskRequest(PipelineTask.FULL_MARKET, later))
    pending = adapter.data.input_quality_status()
    assert adapter.decisions.register_review(projection, observed) is None
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
        candidate_pool_size=120,
        decision_build=_decision_build(now=lambda: clock[0]),
    )
    request = _request(EVALUATED_AT, phase="afternoon")
    if superseded_by in {"date", "resume"}:
        session = TradingSession(lambda: clock[0], lambda: clock[0].timestamp())
        request = replace(request, session=session.capture())
    _prime_scoring_cache(adapter, EVALUATED_AT)
    adapter.data.refresh(request)
    local = adapter.decisions.build_local(request)
    projection = adapter.decisions.projection(local.version)
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
    assert (result.decision is not None) == successful
    if superseded_by == "input":
        adapter.data.refresh_task(PipelineTaskRequest(PipelineTask.FULL_MARKET, EVALUATED_AT + timedelta(seconds=1)))
    elif superseded_by == "local":
        newer = replace(request, sequence=2, input_version="newer-local")
        adapter.data.refresh(newer)
        assert adapter.decisions.build_local(newer).version != local.version
    elif superseded_by == "date":
        clock[0] += timedelta(days=1)
    elif superseded_by == "resume":
        clock[0] += timedelta(minutes=6)
    elif superseded_by == "close":
        clock[0] = clock[0].replace(hour=15, minute=0)
    elif superseded_by in {"cas_close", "cas_input"}:
        from trader.recommendation.application.pipeline.local_score import local_decision_builder

        update = local_decision_builder.UpdateSupplyStatusDecision

        def advance_before_commit(*args, **kwargs):
            quality = update(*args, **kwargs)
            if superseded_by == "cas_close":
                clock[0] = clock[0].replace(hour=15, minute=0)
            else:
                adapter.data.refresh_task(
                    PipelineTaskRequest(PipelineTask.FULL_MARKET, EVALUATED_AT + timedelta(seconds=1))
                )
            return quality

        monkeypatch.setattr(local_decision_builder, "UpdateSupplyStatusDecision", advance_before_commit)
    before = adapter.data.input_quality_status()
    accepted = adapter.decisions.register_review(projection, result)
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
    assert adapter.decisions.projection(local.version) is projection


def test_local_diagnostics_cannot_commit_after_inputs_advance_during_scoring(application_feature_factory, monkeypatch):
    from trader.recommendation.application.pipeline.local_score import local_decision_builder

    adapter = build_cycle_fixture(
        _Market(_features(application_feature_factory)),
        config_version="fixture",
        candidate_pool_size=120,
        decision_build=_decision_build(now=lambda: EVALUATED_AT),
    )
    request = _request(EVALUATED_AT, phase="afternoon")
    _prime_scoring_cache(adapter, EVALUATED_AT)
    adapter.data.refresh(request)
    before = adapter.data.input_quality_status()
    build = local_decision_builder.BuildSupplyStatus

    def advance_before_commit(*args, **kwargs):
        quality = build(*args, **kwargs)
        adapter.data.refresh_task(PipelineTaskRequest(PipelineTask.FULL_MARKET, EVALUATED_AT + timedelta(seconds=1)))
        return quality

    monkeypatch.setattr(local_decision_builder, "BuildSupplyStatus", advance_before_commit)
    local = adapter.decisions.build_local(request)
    assert local is not None
    assert adapter.data.input_quality_status() == before
    projection = adapter.decisions.projection(local.version)
    observed = BuildScoredHybrid(projection, _policy(), {}, review_deadline=request.review_deadline)
    assert observed is not None
    assert adapter.decisions.register_review(projection, observed) is None
    assert adapter.data.input_quality_status() == before
