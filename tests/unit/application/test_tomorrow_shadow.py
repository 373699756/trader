from __future__ import annotations

import threading
import time
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from trader.application.current_decisions import CurrentDecisionIndex
from trader.application.ports.tomorrow_research import TomorrowResearchTraceRecorderPort
from trader.application.recommendation_policy_codec import _freeze_policy
from trader.application.recommendations import RecommendationEngine
from trader.application.tomorrow_events import TomorrowDecisionEventStream
from trader.application.tomorrow_research_projection import build_tomorrow_research_trace
from trader.application.tomorrow_research_trace import (
    AsyncTomorrowResearchTraceRecorder,
    InMemoryTomorrowResearchTraceStore,
    research_trace_payload,
)
from trader.application.tomorrow_research_trace_types import TomorrowResearchTraceCapture
from trader.application.tomorrow_shadow import (
    TomorrowCutoverGate,
    TomorrowCutoverPolicy,
    TomorrowShadowObservation,
)
from trader.application.tomorrow_shadow_projection import (
    native_input_from_snapshot,
    project_tomorrow_input,
    project_tomorrow_snapshot,
)
from trader.application.tomorrow_shadow_runtime import (
    TomorrowShadowDependencies,
    TomorrowShadowRuntime,
    TomorrowShadowWorker,
)
from trader.application.tomorrow_views import (
    TomorrowDecisionQueries,
    TomorrowQuoteOverlayIndex,
)
from trader.application.workers import BoundedExecutor
from trader.bootstrap import _recommendation_policy
from trader.domain.market.models import Board
from trader.domain.recommendation.models import (
    FusionMode,
    RecommendationReplayInput,
    RecommendationSnapshot,
    Strategy,
)
from trader.infra.settings import load_strategy_settings

SHANGHAI = ZoneInfo("Asia/Shanghai")
TRADE_DATE = date(2026, 7, 28)
OBSERVED_AT = datetime(2026, 7, 28, 14, 40, tzinfo=SHANGHAI)
PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_cutover_gate_requires_real_samples_and_matching_freeze() -> None:
    gate = TomorrowCutoverGate(
        TomorrowCutoverPolicy(
            minimum_samples=2,
            minimum_trade_days=1,
            local_publish_p95_seconds=5.0,
            decision_age_p95_seconds=10.0,
        )
    )

    gate.record(
        _observation(
            sequence=1,
            observed_at=datetime(2026, 7, 28, 9, 40, tzinfo=SHANGHAI),
            frozen=False,
        )
    )
    assert gate.status().eligible is False
    assert gate.status().blockers == (
        "insufficient_samples",
        "incomplete_trade_day",
        "matching_freeze_missing",
    )

    gate.record(
        _observation(
            sequence=2,
            observed_at=datetime(2026, 7, 28, 14, 50, tzinfo=SHANGHAI),
            frozen=True,
        )
    )

    status = gate.status()
    assert status.eligible is True
    assert status.blockers == ()
    assert status.sample_count == 2
    assert status.successful_sample_count == 2
    assert status.trade_day_count == 1
    assert status.complete_trade_day_count == 1
    assert status.selection_agreement_ratio == 1.0
    assert status.filter_agreement_ratio == 1.0
    assert status.local_publish_p95_seconds == 0.8
    assert status.decision_age_p95_seconds == 2.0


def test_cutover_gate_reports_every_failed_engineering_condition() -> None:
    gate = TomorrowCutoverGate(TomorrowCutoverPolicy(minimum_samples=1, minimum_trade_days=1))
    gate.record(
        TomorrowShadowObservation(
            trade_date=TRADE_DATE,
            observed_at=OBSERVED_AT,
            baseline_snapshot_id="legacy:1",
            decision_version="decision:1",
            input_version="input:1",
            config_version="runtime:test",
            strategy_version="strategy:test",
            fusion_version="fusion:test",
            decision_schema_version="decision_epoch_v1",
            parent_decision_version="",
            selected_codes_match=False,
            filter_reasons_match=False,
            local_publish_seconds=5.001,
            decision_age_seconds=10.001,
            processing_seconds=0.1,
            deepseek_request_delta=1,
            resource_limits_passed=False,
            baseline_frozen=True,
            v2_frozen=False,
            freeze_codes_match=False,
            freeze_content_hash="",
            processing_error="selection_failed",
        )
    )

    assert gate.status().blockers == (
        "insufficient_samples",
        "incomplete_trade_day",
        "deepseek_request_delta_nonzero",
        "matching_freeze_missing",
        "processing_errors_present",
        "resource_limits_failed",
    )


def test_cutover_gate_keeps_only_bounded_recent_samples() -> None:
    gate = TomorrowCutoverGate(
        TomorrowCutoverPolicy(
            minimum_samples=2,
            minimum_trade_days=1,
            maximum_samples=2,
        )
    )
    gate.record(
        _observation(
            sequence=1,
            observed_at=datetime(2026, 7, 28, 9, 30, tzinfo=SHANGHAI),
            selected_codes_match=False,
        )
    )
    gate.record(
        _observation(
            sequence=2,
            observed_at=datetime(2026, 7, 28, 9, 40, tzinfo=SHANGHAI),
        )
    )
    gate.record(
        _observation(
            sequence=3,
            observed_at=datetime(2026, 7, 28, 14, 50, tzinfo=SHANGHAI),
            frozen=True,
        )
    )

    status = gate.status()
    assert status.sample_count == 2
    assert status.eligible is True
    assert status.selection_agreement_ratio == 1.0


def test_cutover_gate_does_not_count_repeated_baseline_input() -> None:
    gate = TomorrowCutoverGate(TomorrowCutoverPolicy(minimum_samples=2, minimum_trade_days=1))
    repeated = _observation(sequence=1, frozen=True)

    gate.record(repeated)
    gate.record(repeated)

    status = gate.status()
    assert status.sample_count == 1
    assert status.successful_sample_count == 1
    assert status.eligible is False
    assert status.blockers == ("insufficient_samples", "incomplete_trade_day")


def test_cutover_gate_blocks_when_durable_evidence_write_fails() -> None:
    evidence = Mock()
    evidence.record.side_effect = OSError("disk unavailable")
    gate = TomorrowCutoverGate(
        TomorrowCutoverPolicy(minimum_samples=1, minimum_trade_days=1),
        evidence=evidence,
    )

    gate.record(
        _observation(
            sequence=1,
            observed_at=datetime(2026, 7, 28, 9, 40, tzinfo=SHANGHAI),
        )
    )

    status = gate.status()
    assert status.sample_count == 1
    assert status.evidence_failure_count == 1
    assert "evidence_persistence_failed" in status.blockers


def test_cutover_gate_rejects_same_time_identity_conflict() -> None:
    gate = TomorrowCutoverGate(TomorrowCutoverPolicy(minimum_samples=1, minimum_trade_days=1))
    original = _observation(sequence=1)

    gate.record(original)
    gate.record(replace(original, selected_codes_match=False))

    status = gate.status()
    assert status.sample_count == 1
    assert status.selection_agreement_ratio == 1.0
    assert status.evidence_failure_count == 1
    assert "evidence_persistence_failed" in status.blockers


def test_cutover_gate_retains_latest_observation_times_when_input_is_out_of_order() -> None:
    gate = TomorrowCutoverGate(
        TomorrowCutoverPolicy(
            minimum_samples=2,
            minimum_trade_days=1,
            maximum_samples=2,
        )
    )
    gate.record(
        _observation(
            sequence=2,
            observed_at=datetime(2026, 7, 28, 10, 30, tzinfo=SHANGHAI),
        )
    )
    gate.record(
        _observation(
            sequence=3,
            observed_at=datetime(2026, 7, 28, 14, 30, tzinfo=SHANGHAI),
        )
    )
    gate.record(
        _observation(
            sequence=1,
            observed_at=datetime(2026, 7, 28, 9, 30, tzinfo=SHANGHAI),
            selected_codes_match=False,
        )
    )

    status = gate.status()
    assert status.sample_count == 2
    assert status.selection_agreement_ratio == 1.0


def test_shadow_failure_keeps_future_snapshot_trade_date(application_feature_factory) -> None:
    previous_day = datetime(2026, 7, 27, 23, 59, tzinfo=SHANGHAI)
    gate = TomorrowCutoverGate(TomorrowCutoverPolicy(minimum_samples=1, minimum_trade_days=1))
    decisions = Mock(latest=Mock(return_value=None))
    policy = _recommendation_policy(load_strategy_settings(PROJECT_ROOT / "config" / "v2" / "strategy.json"))
    runtime = TomorrowShadowRuntime(
        policy,
        TomorrowShadowDependencies(
            decisions,
            Mock(),
            Mock(),
            Mock(),
            Mock(),
            gate,
            Mock(now=Mock(return_value=previous_day)),
        ),
    )
    snapshot = _baseline_snapshot(policy, application_feature_factory)

    assert runtime.process(snapshot) is False
    decisions.publish.assert_not_called()

    status = gate.status()
    assert status.sample_count == 1
    assert status.trade_day_count == 0
    assert status.processing_error_count == 1
    assert status.blockers == (
        "insufficient_samples",
        "incomplete_trade_day",
        "matching_freeze_missing",
        "processing_errors_present",
    )


def test_shadow_skips_restored_previous_trade_day_without_recording_failure(
    application_feature_factory,
) -> None:
    policy = _recommendation_policy(load_strategy_settings(PROJECT_ROOT / "config" / "v2" / "strategy.json"))
    gate = TomorrowCutoverGate(TomorrowCutoverPolicy(minimum_samples=1, minimum_trade_days=1))
    runtime = TomorrowShadowRuntime(
        policy,
        TomorrowShadowDependencies(
            Mock(latest=Mock(return_value=None)),
            Mock(),
            Mock(),
            Mock(),
            Mock(),
            gate,
            Mock(now=Mock(return_value=datetime(2026, 7, 29, 12, 30, tzinfo=SHANGHAI))),
        ),
    )
    restored = replace(
        _baseline_snapshot(policy, application_feature_factory),
        frozen=True,
    )

    assert runtime.process(restored) is True

    status = runtime.status()
    assert status["baseline_stale_trade_date_skipped"] == 1
    assert status["failed"] == 0
    assert status["last_error"] == ""
    assert status["cutover_gate"]["retained_sample_count"] == 0
    assert status["cutover_gate"]["sample_count"] == 0
    assert status["cutover_gate"]["processing_error_count"] == 0


def test_cutover_gate_evaluates_latest_complete_day_without_cross_day_contamination() -> None:
    gate = TomorrowCutoverGate(
        TomorrowCutoverPolicy(
            minimum_samples=2,
            minimum_trade_days=1,
        )
    )
    incomplete_day = TRADE_DATE - timedelta(days=1)
    gate.record(
        replace(
            _observation(
                sequence=1,
                observed_at=datetime(2026, 7, 27, 12, 0, tzinfo=SHANGHAI),
            ),
            trade_date=incomplete_day,
            processing_error="startup_stale_baseline",
        )
    )
    gate.record(
        _observation(
            sequence=2,
            observed_at=datetime(2026, 7, 28, 9, 30, tzinfo=SHANGHAI),
        )
    )
    gate.record(
        _observation(
            sequence=3,
            observed_at=datetime(2026, 7, 28, 14, 50, tzinfo=SHANGHAI),
            frozen=True,
        )
    )

    status = gate.status()
    assert status.eligible is True
    assert status.evaluation_trade_date == TRADE_DATE.isoformat()
    assert status.retained_sample_count == 3
    assert status.sample_count == 2
    assert status.successful_sample_count == 2
    assert status.trade_day_count == 1
    assert status.complete_trade_day_count == 1
    assert status.processing_error_count == 0


def test_shadow_projection_reuses_replay_input_without_an_external_port(
    application_feature_factory,
) -> None:
    policy = _recommendation_policy(load_strategy_settings(PROJECT_ROOT / "config" / "v2" / "strategy.json"))
    features = tuple(
        application_feature_factory(code, OBSERVED_AT)
        for code in ("600001", "600002", "300001", "300002", "688001", "688002")
    )
    replay = RecommendationReplayInput(
        schema_version="recommendation_replay_v4",
        algorithm_version="v16_board_scoring_v2",
        policy=_freeze_policy(policy),
        evaluated_at=OBSERVED_AT,
        market_features=features,
        requested_codes=tuple(item.quote.code for item in features),
        candidate_features=features,
        reviews={},
        preselect_max_age_seconds=10.0,
        score_max_age_seconds=10.0,
        candidate_pool_size=120,
    )
    baseline = RecommendationSnapshot(
        snapshot_id="legacy:tomorrow:1",
        strategy=Strategy.TOMORROW,
        trade_date=TRADE_DATE.isoformat(),
        phase="afternoon",
        data_version="legacy-input:1",
        strategy_version=policy.strategy_version,
        fusion_version=policy.fusion_version,
        fusion_mode=FusionMode.LOCAL_DEGRADED,
        published_at=OBSERVED_AT,
        recommendations=(),
        filtered_count=0,
        filter_reasons={},
        config_version="runtime:test",
        replay_input=replay,
    )

    projection = project_tomorrow_snapshot(
        baseline,
        policy,
        decision_sequence=4,
    )

    assert projection.local.sequence == 4
    assert projection.local.projection_stage == "local"
    assert projection.local.trade_date == TRADE_DATE
    assert projection.hybrid is None
    assert projection.input_version.startswith("native-input:")


def test_native_input_projects_same_local_identity_before_v1_snapshot(
    application_feature_factory,
) -> None:
    policy = _recommendation_policy(load_strategy_settings(PROJECT_ROOT / "config" / "v2" / "strategy.json"))
    baseline = _baseline_snapshot(policy, application_feature_factory)
    native_input = native_input_from_snapshot(baseline)

    native = project_tomorrow_input(native_input, policy, decision_sequence=4)
    mirrored = project_tomorrow_snapshot(
        replace(
            baseline,
            snapshot_id="legacy:tomorrow:later",
        ),
        policy,
        decision_sequence=4,
    )

    assert native.local.version == mirrored.local.version
    assert native.input_version == mirrored.input_version
    assert native.hybrid is None


def test_native_transient_empty_retains_last_valid_decision(
    application_feature_factory,
) -> None:
    policy = _recommendation_policy(load_strategy_settings(PROJECT_ROOT / "config" / "v2" / "strategy.json"))
    evaluated_at = OBSERVED_AT + timedelta(seconds=31)
    runtime, decisions, queries, events = _native_runtime(policy, evaluated_at)
    baseline = _baseline_snapshot(policy, application_feature_factory)

    assert runtime.process_native(native_input_from_snapshot(baseline)) is True
    retained = decisions.latest()
    assert retained is not None
    event_sequence = events.status().sequence
    stale_features = tuple(
        replace(
            feature,
            quote=replace(
                feature.quote,
                source_time=OBSERVED_AT,
                received_time=OBSERVED_AT,
            ),
            observed_at=OBSERVED_AT,
        )
        for feature in baseline.replay_input.market_features
    )
    stale = replace(
        native_input_from_snapshot(baseline),
        evaluated_at=evaluated_at,
        market_features=stale_features,
        candidate_features=stale_features,
    )

    assert runtime.process_native(stale) is True
    stale_baseline = replace(
        baseline,
        replay_input=replace(
            baseline.replay_input,
            evaluated_at=evaluated_at,
            market_features=stale_features,
            candidate_features=stale_features,
        ),
    )
    assert runtime.process(stale_baseline) is True

    status = runtime.status()
    assert decisions.latest() is retained
    assert events.status().sequence == event_sequence
    assert queries.current().decision_version == retained.version
    assert status["transient_invalid_empty_count"] == 1
    assert status["last_valid_decision_retained_count"] == 1
    assert status["cold_start_not_ready_count"] == 0
    assert status["baseline_invalid_input_skipped"] == 1
    assert status["baseline_fallbacks"] == 0
    assert status["last_input_quality"]["status"] == "transient_invalid_empty"
    assert status["last_input_quality"]["candidate_transient_reason_counts"] == {
        "stale_quote": 6,
    }
    assert status["last_error"] == "transient_invalid_empty:last_valid_decision_retained"


def test_native_transient_empty_cold_start_remains_not_ready(
    application_feature_factory,
) -> None:
    policy = _recommendation_policy(load_strategy_settings(PROJECT_ROOT / "config" / "v2" / "strategy.json"))
    evaluated_at = OBSERVED_AT + timedelta(seconds=31)
    runtime, decisions, queries, events = _native_runtime(policy, evaluated_at)
    baseline = _baseline_snapshot(policy, application_feature_factory)
    stale_features = tuple(
        replace(
            feature,
            quote=replace(
                feature.quote,
                source_time=OBSERVED_AT,
                received_time=OBSERVED_AT,
            ),
            observed_at=OBSERVED_AT,
        )
        for feature in baseline.replay_input.market_features
    )
    stale = replace(
        native_input_from_snapshot(baseline),
        evaluated_at=evaluated_at,
        market_features=stale_features,
        candidate_features=stale_features,
    )

    assert runtime.process_native(stale) is True

    status = runtime.status()
    assert decisions.latest() is None
    assert events.status().sequence == 0
    assert queries.current().status == "not_ready"
    assert queries.status().recent_failures == ("transient_invalid_empty:not_ready",)
    assert status["transient_invalid_empty_count"] == 1
    assert status["last_valid_decision_retained_count"] == 0
    assert status["cold_start_not_ready_count"] == 1
    assert status["last_input_quality"]["status"] == "transient_invalid_empty"
    assert status["last_error"] == "transient_invalid_empty:not_ready"


def test_baseline_only_transient_empty_cannot_publish_through_fallback(
    application_feature_factory,
) -> None:
    policy = _recommendation_policy(load_strategy_settings(PROJECT_ROOT / "config" / "v2" / "strategy.json"))
    evaluated_at = OBSERVED_AT + timedelta(seconds=31)
    runtime, decisions, queries, events = _native_runtime(policy, evaluated_at)
    baseline = _baseline_snapshot(policy, application_feature_factory)
    stale_features = tuple(
        replace(
            feature,
            quote=replace(
                feature.quote,
                source_time=OBSERVED_AT,
                received_time=OBSERVED_AT,
            ),
            observed_at=OBSERVED_AT,
        )
        for feature in baseline.replay_input.market_features
    )
    stale_baseline = replace(
        baseline,
        replay_input=replace(
            baseline.replay_input,
            evaluated_at=evaluated_at,
            market_features=stale_features,
            candidate_features=stale_features,
        ),
    )

    assert runtime.process(stale_baseline) is True

    status = runtime.status()
    assert decisions.latest() is None
    assert events.status().sequence == 0
    assert queries.current().status == "not_ready"
    assert status["baseline_fallbacks"] == 0
    assert status["baseline_invalid_input_skipped"] == 1
    assert status["last_input_quality"]["status"] == "transient_invalid_empty"


def test_native_complete_business_empty_is_publishable(
    application_feature_factory,
) -> None:
    policy = _recommendation_policy(load_strategy_settings(PROJECT_ROOT / "config" / "v2" / "strategy.json"))
    runtime, decisions, queries, events = _native_runtime(policy, OBSERVED_AT)
    baseline = _baseline_snapshot(policy, application_feature_factory)
    rejected = tuple(
        replace(feature, quote=replace(feature.quote, is_st=True)) for feature in baseline.replay_input.market_features
    )
    native_input = replace(
        native_input_from_snapshot(baseline),
        market_features=rejected,
        candidate_features=rejected,
    )

    assert runtime.process_native(native_input) is True

    status = runtime.status()
    assert decisions.latest() is not None
    assert events.status().sequence == 1
    assert queries.current().status == "ready"
    assert queries.current().items == ()
    assert status["business_empty_published_count"] == 1
    assert status["last_input_quality"]["status"] == "business_empty"
    assert status["last_input_quality"]["candidate_rejected_count"] == 6
    assert status["last_error"] == ""


def test_shadow_projection_separates_hard_filter_comparison_from_v2_audit(
    application_feature_factory,
) -> None:
    policy = _recommendation_policy(load_strategy_settings(PROJECT_ROOT / "config" / "v2" / "strategy.json"))
    baseline = _baseline_snapshot(policy, application_feature_factory)
    first = baseline.replay_input.market_features[0]
    market_features = (
        replace(first, quote=replace(first.quote, is_st=True)),
        *baseline.replay_input.market_features[1:],
    )
    native_input = replace(
        native_input_from_snapshot(baseline),
        market_features=market_features,
        candidate_features=market_features[1:],
    )

    projection = project_tomorrow_input(native_input, policy, decision_sequence=4)

    assert projection.hard_filter_reason_counts["st_or_delisting"] == 1
    assert "board_data_reliability_below_threshold" not in projection.hard_filter_reason_counts
    assert "board_data_reliability_below_threshold" in projection.local.filter_reason_counts


def test_research_trace_keeps_all_hard_filter_passes_and_only_aggregates_rejections(
    application_feature_factory,
) -> None:
    policy = _recommendation_policy(load_strategy_settings(PROJECT_ROOT / "config" / "v2" / "strategy.json"))
    baseline = _baseline_snapshot(policy, application_feature_factory)
    rejected_code = baseline.replay_input.market_features[0].quote.code
    rejected_name = baseline.replay_input.market_features[0].quote.name
    market_features = (
        replace(
            baseline.replay_input.market_features[0],
            quote=replace(baseline.replay_input.market_features[0].quote, is_st=True),
        ),
        *baseline.replay_input.market_features[1:],
    )
    native_input = replace(
        native_input_from_snapshot(baseline),
        market_features=market_features,
        candidate_features=market_features[1:3],
    )
    projection = project_tomorrow_input(native_input, policy, decision_sequence=4)

    trace = build_tomorrow_research_trace(projection, baseline_snapshot_id="baseline-001")
    payload = research_trace_payload(trace)

    assert rejected_code.encode() not in payload
    assert rejected_name.encode() not in payload
    assert len(trace.passed_candidates) == len(market_features) - 1
    assert sum(item.count for item in trace.hard_filter_aggregates if item.reason == "st_or_delisting") == 1
    assert all(item.candidate_components for item in trace.passed_candidates)
    assert sum(item.production_top120 for item in trace.passed_candidates) == 2
    assert any(item.pruning_reason == "production_preselection_excluded" for item in trace.passed_candidates)
    assert all(item.upper_bound_status == "not_computed" for item in trace.passed_candidates)
    assert all(item.downside_status in {"pass", "observe"} for item in trace.production_local.candidates)
    assert trace.production_local.variant == "production_local"
    assert trace.research_shadow.variant == "research_shadow"
    assert trace.deepseek_request_delta == 0

    store = InMemoryTomorrowResearchTraceStore()
    executor = BoundedExecutor(worker_count=1, queue_capacity=1, thread_name_prefix="trace-test")
    recorder = AsyncTomorrowResearchTraceRecorder(store, executor)
    executor.start()
    try:
        assert recorder.enqueue(TomorrowResearchTraceCapture(projection, "baseline-001")).status == "queued"
    finally:
        executor.stop(wait=True)
    assert recorder.status().completed == 1
    assert store.get(projection.input_version) == trace


def test_native_input_quality_scopes_optional_risk_to_explicit_candidates(
    application_feature_factory,
) -> None:
    policy = _recommendation_policy(load_strategy_settings(PROJECT_ROOT / "config" / "v2" / "strategy.json"))
    baseline = _baseline_snapshot(policy, application_feature_factory)
    degraded = tuple(
        replace(
            feature,
            quote=replace(
                feature.quote,
                execution_restrictions=(
                    "board_identity_degraded",
                    "missing_listing_age_sessions",
                    "missing_listing_date",
                ),
            ),
        )
        for feature in baseline.replay_input.market_features
    )
    native_input = replace(
        native_input_from_snapshot(baseline),
        market_features=degraded,
        candidate_features=degraded[:2],
    )

    projection = project_tomorrow_input(native_input, policy, decision_sequence=4)

    quality = projection.input_quality
    assert quality.population_count == 6
    assert quality.candidate_count == 2
    assert quality.candidate_optional_reason_counts["board_identity_degraded"] == 2
    assert quality.candidate_optional_reason_counts["board_data_reliability_below_threshold"] == 2
    assert quality.candidate_optional_reason_counts["missing_listing_date"] == 2
    assert quality.candidate_optional_reason_counts["missing_listing_age_sessions"] == 2
    assert projection.local.degraded_reasons == (
        "board_data_reliability_below_threshold",
        "board_identity_degraded",
        "missing_listing_age_sessions",
        "missing_listing_date",
    )


def test_native_projection_matches_v1_three_board_decisions_for_same_candidate_batch(
    application_feature_factory,
) -> None:
    configured = _recommendation_policy(load_strategy_settings(PROJECT_ROOT / "config" / "v2" / "strategy.json"))
    policy = replace(
        configured,
        selection=replace(
            configured.selection,
            thresholds={**configured.selection.thresholds, "tomorrow": 0.0},
        ),
    )
    engine = RecommendationEngine(policy)
    market_features = tuple(
        application_feature_factory(
            f"{prefix}{index:03d}",
            OBSERVED_AT,
            industry=f"industry-{index}",
        )
        for prefix in ("600", "300", "688")
        for index in range(120)
    )
    candidates, reasons, details = engine.preselect(
        market_features,
        now=OBSERVED_AT,
        max_age_seconds=30.0,
        limit=360,
        strategies=(Strategy.TOMORROW,),
        trade_date=TRADE_DATE.isoformat(),
        phase="afternoon",
    )
    prepared = engine.prepare_snapshot(
        Strategy.TOMORROW,
        candidates,
        now=OBSERVED_AT,
        phase="afternoon",
        trade_date=TRADE_DATE.isoformat(),
        data_version="candidate-data:shared",
        review_deadline=OBSERVED_AT,
        max_age_seconds=30.0,
        filtered_count=len({item.stock_code for item in details}),
        filter_reasons=reasons,
        filter_details=details,
        target_prices=None,
        long_groups=(),
        market_features=market_features,
        requested_codes=tuple(item.quote.code for item in candidates),
        preselect_max_age_seconds=30.0,
        candidate_pool_size=360,
    )
    baseline = replace(
        engine.finalize_snapshot(prepared, {}, projection_stage="local"),
        config_version="runtime:test",
    )

    projection = project_tomorrow_input(
        native_input_from_snapshot(baseline),
        policy,
        decision_sequence=4,
    )

    assert baseline.recommendations
    assert {item.quote.board for item in candidates} == {Board.MAIN, Board.CHINEXT, Board.STAR}
    baseline_selected = {item.features.quote.code: item for item in baseline.recommendations}
    native_selected = {item.code: item for item in projection.local.entries if item.selected}
    assert tuple(baseline_selected) == tuple(native_selected)
    assert {
        code: (
            item.score.local_score,
            item.action,
            item.action_reason,
            item.rank,
            item.veto,
            item.local_risk_facts,
        )
        for code, item in baseline_selected.items()
    } == {
        code: (
            item.score.local_score,
            item.action,
            item.action_reason,
            item.rank,
            item.veto,
            item.local_risk_facts,
        )
        for code, item in native_selected.items()
    }
    assert dict(baseline.filter_reasons) == dict(projection.hard_filter_reason_counts)


def test_repeated_local_baseline_does_not_downgrade_or_fail_current_hybrid(
    application_feature_factory,
) -> None:
    policy = _recommendation_policy(load_strategy_settings(PROJECT_ROOT / "config" / "v2" / "strategy.json"))
    projection = project_tomorrow_input(
        native_input_from_snapshot(_baseline_snapshot(policy, application_feature_factory)),
        policy,
        decision_sequence=4,
    )
    current_hybrid = replace(
        projection.local,
        sequence=5,
        projection_stage="hybrid",
        parent_decision_version=projection.local.version,
    )
    decisions = Mock()
    decisions.latest.return_value = current_hybrid
    runtime = TomorrowShadowRuntime(
        policy,
        TomorrowShadowDependencies(
            decisions,
            Mock(),
            Mock(),
            Mock(),
            Mock(),
            Mock(),
            Mock(),
        ),
    )

    effective = runtime._publish_effective_projection(projection)

    assert effective == projection.local
    decisions.publish.assert_not_called()
    assert runtime._baseline_superseded == 0


def test_second_distinct_hybrid_for_same_local_is_superseded_without_failure(
    application_feature_factory,
) -> None:
    policy = _recommendation_policy(load_strategy_settings(PROJECT_ROOT / "config" / "v2" / "strategy.json"))
    local_projection = project_tomorrow_input(
        native_input_from_snapshot(_baseline_snapshot(policy, application_feature_factory)),
        policy,
        decision_sequence=4,
    )
    current_hybrid = replace(
        local_projection.local,
        sequence=5,
        projection_stage="hybrid",
        parent_decision_version=local_projection.local.version,
        degraded_reasons=("deepseek_incomplete",),
    )
    later_hybrid = replace(current_hybrid, degraded_reasons=())
    decisions = Mock()
    decisions.latest.return_value = current_hybrid
    runtime = TomorrowShadowRuntime(
        policy,
        TomorrowShadowDependencies(
            decisions,
            Mock(),
            Mock(),
            Mock(),
            Mock(),
            Mock(),
            Mock(),
        ),
    )

    effective = runtime._publish_effective_projection(replace(local_projection, hybrid=later_hybrid))

    assert effective is None
    decisions.publish.assert_not_called()
    assert runtime._baseline_superseded == 1
    assert runtime._failed == 0


def test_native_input_identity_does_not_change_with_snapshot_or_reviews(
    application_feature_factory,
) -> None:
    policy = _recommendation_policy(load_strategy_settings(PROJECT_ROOT / "config" / "v2" / "strategy.json"))
    baseline = _baseline_snapshot(policy, application_feature_factory)
    first = native_input_from_snapshot(baseline)
    second = native_input_from_snapshot(
        replace(
            baseline,
            snapshot_id="legacy:tomorrow:hybrid",
            replay_input=replace(baseline.replay_input, reviews={}),
        )
    )

    assert first.input_version == second.input_version


def test_native_input_identity_is_canonical_across_feature_order(
    application_feature_factory,
) -> None:
    policy = _recommendation_policy(load_strategy_settings(PROJECT_ROOT / "config" / "v2" / "strategy.json"))
    native_input = native_input_from_snapshot(_baseline_snapshot(policy, application_feature_factory))

    reordered = replace(
        native_input,
        market_features=tuple(reversed(native_input.market_features)),
        requested_codes=tuple(reversed(native_input.requested_codes)),
        candidate_features=tuple(reversed(native_input.candidate_features)),
    )

    assert reordered.input_version == native_input.input_version


def test_native_input_identity_tracks_feature_content_without_merge_epoch(
    application_feature_factory,
) -> None:
    policy = _recommendation_policy(load_strategy_settings(PROJECT_ROOT / "config" / "v2" / "strategy.json"))
    baseline = _baseline_snapshot(policy, application_feature_factory)
    first = native_input_from_snapshot(baseline)
    changed_feature = replace(
        first.candidate_features[0],
        values={**first.candidate_features[0].values, "trend_score": 99.0},
        merge_epoch="",
    )
    unchanged_identity_feature = replace(first.candidate_features[0], merge_epoch="")

    changed = replace(
        first,
        candidate_features=(changed_feature, *first.candidate_features[1:]),
    )
    unchanged = replace(
        first,
        candidate_features=(unchanged_identity_feature, *first.candidate_features[1:]),
    )

    assert changed.input_version != unchanged.input_version


def test_shadow_worker_is_latest_wins_and_stops_cleanly() -> None:
    started = threading.Event()
    release = threading.Event()
    processed: list[str] = []

    def process(snapshot) -> bool:
        processed.append(snapshot.snapshot_id)
        if snapshot.snapshot_id == "first":
            started.set()
            assert release.wait(2.0)
        return True

    processor = Mock()
    processor.process.side_effect = process
    worker = TomorrowShadowWorker(processor)
    first = _shadow_snapshot_mock("first")
    second = _shadow_snapshot_mock("second")
    third = _shadow_snapshot_mock("third")

    assert worker.start() is True
    assert worker.offer(first) is True
    assert started.wait(1.0)
    assert worker.offer(second) is True
    assert worker.offer(third) is True
    release.set()
    deadline = time.monotonic() + 2.0
    while worker.status()["completed"] != 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    worker.stop(wait=True, timeout_seconds=1.0)

    assert processed == ["first", "third"]
    assert worker.status() == {
        "running": False,
        "pending": False,
        "offered": 3,
        "native_offered": 0,
        "baseline_offered": 3,
        "replaced": 1,
        "completed": 2,
        "failed": 0,
        "last_error": "",
        "capacity": 1,
    }


def test_shadow_worker_dispatches_native_input_without_waiting_for_baseline(
    application_feature_factory,
) -> None:
    policy = _recommendation_policy(load_strategy_settings(PROJECT_ROOT / "config" / "v2" / "strategy.json"))
    native_input = native_input_from_snapshot(_baseline_snapshot(policy, application_feature_factory))
    processor = Mock()
    processor.process_native.return_value = True
    worker = TomorrowShadowWorker(processor)

    assert worker.start() is True
    assert worker.offer_native(native_input) is True
    deadline = time.monotonic() + 2.0
    while worker.status()["completed"] != 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    worker.stop(wait=True, timeout_seconds=1.0)

    processor.process_native.assert_called_once_with(native_input)
    processor.process.assert_not_called()
    assert worker.status()["native_offered"] == 1
    assert worker.status()["baseline_offered"] == 0


def _baseline_snapshot(policy, application_feature_factory) -> RecommendationSnapshot:
    features = tuple(
        application_feature_factory(code, OBSERVED_AT)
        for code in ("600001", "600002", "300001", "300002", "688001", "688002")
    )
    replay = RecommendationReplayInput(
        schema_version="recommendation_replay_v4",
        algorithm_version="v16_board_scoring_v2",
        policy=_freeze_policy(policy),
        evaluated_at=OBSERVED_AT,
        market_features=features,
        requested_codes=tuple(item.quote.code for item in features),
        candidate_features=features,
        reviews={},
        preselect_max_age_seconds=10.0,
        score_max_age_seconds=10.0,
        candidate_pool_size=120,
    )
    return RecommendationSnapshot(
        snapshot_id="legacy:tomorrow:native",
        strategy=Strategy.TOMORROW,
        trade_date=TRADE_DATE.isoformat(),
        phase="afternoon",
        data_version="legacy-input:native",
        strategy_version=policy.strategy_version,
        fusion_version=policy.fusion_version,
        fusion_mode=FusionMode.LOCAL_DEGRADED,
        published_at=OBSERVED_AT,
        recommendations=(),
        filtered_count=0,
        filter_reasons={},
        config_version="runtime:test",
        replay_input=replay,
    )


def _native_runtime(
    policy,
    now: datetime,
    research_trace: TomorrowResearchTraceRecorderPort | None = None,
) -> tuple[
    TomorrowShadowRuntime,
    CurrentDecisionIndex,
    TomorrowDecisionQueries,
    TomorrowDecisionEventStream,
]:
    decisions = CurrentDecisionIndex()
    quotes = TomorrowQuoteOverlayIndex(decisions)
    events = TomorrowDecisionEventStream()
    clock = Mock(now=Mock(return_value=now))
    runtime_holder: list[TomorrowShadowRuntime] = []
    queries = TomorrowDecisionQueries(
        decisions,
        Mock(load_frozen=Mock(return_value=None)),
        clock,
        quotes=quotes,
        telemetry=lambda: runtime_holder[0].telemetry(),
    )
    runtime = TomorrowShadowRuntime(
        policy,
        TomorrowShadowDependencies(
            decisions,
            quotes,
            events,
            queries,
            Mock(),
            TomorrowCutoverGate(TomorrowCutoverPolicy()),
            clock,
            research_trace,
        ),
    )
    runtime_holder.append(runtime)
    return runtime, decisions, queries, events


def test_research_trace_record_failures_do_not_block_native_path(application_feature_factory) -> None:
    policy = _recommendation_policy(load_strategy_settings(PROJECT_ROOT / "config" / "v2" / "strategy.json"))
    failing_trace = Mock()
    failing_trace.enqueue.side_effect = OSError("trace unavailable")
    baseline = _baseline_snapshot(policy, application_feature_factory)
    runtime, decisions, queries, _ = _native_runtime(policy, OBSERVED_AT, research_trace=failing_trace)

    assert runtime.process_native(native_input_from_snapshot(baseline)) is True
    assert runtime.process(baseline) is True

    assert decisions.latest() is not None
    assert queries.current().status == "ready"
    assert runtime.status()["failed"] == 0
    assert not any(key.startswith("research_trace_") for key in runtime.status())


def _observation(
    *,
    sequence: int,
    observed_at: datetime | None = None,
    frozen: bool = False,
    selected_codes_match: bool = True,
) -> TomorrowShadowObservation:
    return TomorrowShadowObservation(
        trade_date=TRADE_DATE,
        observed_at=observed_at or OBSERVED_AT + timedelta(seconds=sequence),
        baseline_snapshot_id=f"legacy:{sequence}",
        decision_version=f"decision:{sequence}",
        input_version=f"input:{sequence}",
        config_version="runtime:test",
        strategy_version="strategy:test",
        fusion_version="fusion:test",
        decision_schema_version="decision_epoch_v1",
        parent_decision_version="",
        selected_codes_match=selected_codes_match,
        filter_reasons_match=True,
        local_publish_seconds=0.8,
        decision_age_seconds=2.0,
        processing_seconds=0.1,
        deepseek_request_delta=0,
        resource_limits_passed=True,
        baseline_frozen=frozen,
        v2_frozen=frozen,
        freeze_codes_match=frozen,
        freeze_content_hash="a" * 64 if frozen else "",
    )


def _shadow_snapshot_mock(snapshot_id: str):
    snapshot = Mock()
    snapshot.strategy = Strategy.TOMORROW
    snapshot.replay_input = object()
    snapshot.snapshot_id = snapshot_id
    return snapshot
