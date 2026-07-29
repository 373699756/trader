from __future__ import annotations

import threading
import time
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from trader.application.recommendation_policy_codec import _freeze_policy
from trader.application.recommendations import RecommendationEngine
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
