from __future__ import annotations

import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from trader.application.recommendation_policy_codec import _freeze_policy
from trader.application.tomorrow_shadow import (
    TomorrowCutoverGate,
    TomorrowCutoverPolicy,
    TomorrowShadowObservation,
)
from trader.application.tomorrow_shadow_projection import project_tomorrow_snapshot
from trader.application.tomorrow_shadow_runtime import (
    TomorrowShadowDependencies,
    TomorrowShadowRuntime,
    TomorrowShadowWorker,
)
from trader.bootstrap import _recommendation_policy
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

    gate.record(_observation(sequence=1, frozen=False))
    assert gate.status().eligible is False
    assert gate.status().blockers == ("insufficient_samples", "matching_freeze_missing")

    gate.record(_observation(sequence=2, frozen=True))

    status = gate.status()
    assert status.eligible is True
    assert status.blockers == ()
    assert status.sample_count == 2
    assert status.successful_sample_count == 2
    assert status.trade_day_count == 1
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
            selected_codes_match=False,
            filter_reasons_match=False,
            local_publish_seconds=5.001,
            decision_age_seconds=10.001,
            deepseek_request_delta=1,
            resource_limits_passed=False,
            baseline_frozen=True,
            v2_frozen=False,
            freeze_codes_match=False,
            processing_error="selection_failed",
        )
    )

    assert gate.status().blockers == (
        "insufficient_samples",
        "insufficient_trade_days",
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
    gate.record(_observation(sequence=1, selected_codes_match=False))
    gate.record(_observation(sequence=2))
    gate.record(_observation(sequence=3, frozen=True))

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
    assert status.blockers == ("insufficient_samples",)


def test_shadow_failure_keeps_snapshot_trade_date_after_midnight() -> None:
    next_day = datetime(2026, 7, 29, 0, 1, tzinfo=SHANGHAI)
    gate = TomorrowCutoverGate(TomorrowCutoverPolicy(minimum_samples=1, minimum_trade_days=1))
    runtime = TomorrowShadowRuntime(
        Mock(),
        TomorrowShadowDependencies(
            Mock(latest=Mock(return_value=None)),
            Mock(),
            Mock(),
            Mock(),
            Mock(),
            gate,
            Mock(now=Mock(return_value=next_day)),
        ),
    )
    snapshot = _shadow_snapshot_mock("cross-midnight")
    snapshot.trade_date = TRADE_DATE.isoformat()
    snapshot.published_at = OBSERVED_AT
    snapshot.replay_input = None

    assert runtime.process(snapshot) is False

    status = gate.status()
    assert status.sample_count == 1
    assert status.trade_day_count == 0
    assert status.processing_error_count == 1
    assert status.blockers == (
        "insufficient_samples",
        "insufficient_trade_days",
        "matching_freeze_missing",
        "processing_errors_present",
    )


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
    assert projection.input_version.startswith("shadow-input:")


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
        "replaced": 1,
        "completed": 2,
        "failed": 0,
        "last_error": "",
        "capacity": 1,
    }


def _observation(
    *,
    sequence: int,
    frozen: bool = False,
    selected_codes_match: bool = True,
) -> TomorrowShadowObservation:
    return TomorrowShadowObservation(
        trade_date=TRADE_DATE,
        observed_at=OBSERVED_AT + timedelta(seconds=sequence),
        baseline_snapshot_id=f"legacy:{sequence}",
        decision_version=f"decision:{sequence}",
        input_version=f"input:{sequence}",
        selected_codes_match=selected_codes_match,
        filter_reasons_match=True,
        local_publish_seconds=0.8,
        decision_age_seconds=2.0,
        deepseek_request_delta=0,
        resource_limits_passed=True,
        baseline_frozen=frozen,
        v2_frozen=frozen,
        freeze_codes_match=frozen,
    )


def _shadow_snapshot_mock(snapshot_id: str):
    snapshot = Mock()
    snapshot.strategy = Strategy.TOMORROW
    snapshot.replay_input = object()
    snapshot.snapshot_id = snapshot_id
    return snapshot
