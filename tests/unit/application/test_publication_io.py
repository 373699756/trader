from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from pathlib import Path

import pytest

from tests.unit.application.test_tomorrow_freezing import _assert_formal_current_and_history, _at, _Clock
from tests.unit.domain.test_decision_identity import decision
from trader.recommendation.application.pipeline.freeze_publish.freeze_coordinator import (
    DecisionRuntimeIdentity,
    ScoredFreezeCoordinator,
)
from trader.recommendation.application.pipeline.freeze_publish.publication_io import (
    PublicationIoCompletion,
    PublicationIoTarget,
    PublicationIoTracker,
    PublicationOperation,
    observe_publication_io,
)
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import UnifiedDecisionIndex
from trader.recommendation.application.ports.decision_records import DecisionCheckpoint, DecisionRecordUnavailableError
from trader.recommendation.domain.evidence.pipeline import SourceHealthState, StageState
from trader.recommendation.domain.publication.decision_identity import formal_scored_decision
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.infra.persistence.decision_records import SQLiteDecisionRecords
from trader.recommendation.infra.status_projection import publication_io_payload


class _Monotonic:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        self.value += 0.125
        return self.value


def _freezer(index, records, clock, tracker, strategy):
    return ScoredFreezeCoordinator(
        index,
        records,
        clock,
        runtime_identity=DecisionRuntimeIdentity("config-current", "strategy-current", "fusion-current"),
        strategy=strategy,
        publication_io=tracker,
    )


def _receipt(tracker: PublicationIoTracker, operation: PublicationOperation):
    return next(item for item in tracker.snapshots() if item.operation == operation)


@pytest.mark.parametrize("strategy", (Strategy.TOMORROW, Strategy.D25))
@pytest.mark.parametrize("cold", (False, True), ids=("hot", "cold"))
@pytest.mark.parametrize("hour,minute", ((9, 25), (11, 45), (14, 50), (15, 0), (15, 5)))
def test_five_period_io_observation_and_cold_recovery(tmp_path: Path, strategy, cold, hour, minute) -> None:
    clock = _Clock(_at(hour, minute))
    tracker = PublicationIoTracker(now=clock.now, monotonic=_Monotonic())
    index = UnifiedDecisionIndex()
    records = SQLiteDecisionRecords(tmp_path)
    records.initialize()
    current = replace(decision(strategy), observed_at=_at(14, 59, 35))
    original_hash = current.content_hash
    if not cold:
        assert index.publish(current, expected_version=None).accepted
    freezer = _freezer(index, records, clock, tracker, strategy)
    result = freezer.freeze_scheduled()
    if hour < 15:
        assert result.status == "before_freeze"
        assert tracker.snapshots() == ()  # No pretend zero-duration I/O.
        return
    if cold:
        assert result.status == "no_eligible_decision"
        missing = _receipt(tracker, "formal_lookup")
        assert missing.pending_count == 1 and missing.failed_count == 0
        assert missing.source_health.state is SourceHealthState.READY
        assert missing.target.decision_version is None
        result = freezer.freeze_close_fallback(
            current, recovery_path="close_rebuild", official_close_version="official-close:fixture"
        )
    assert result.status == "frozen" and result.record is not None
    assert current.content_hash == original_hash
    for operation in ("freeze_seal", "formal_write", "formal_publish"):
        receipt = _receipt(tracker, operation)
        assert receipt.state is StageState.READY
        assert receipt.input_count == receipt.output_count == 1
        assert receipt.failed_count == receipt.pending_count == receipt.rejected_count == 0
        assert receipt.latency_ms == 125
    _assert_formal_current_and_history(index, records, clock, result.record)
    cold_index = UnifiedDecisionIndex()
    cold_tracker = PublicationIoTracker(now=clock.now, monotonic=_Monotonic())
    restored = _freezer(cold_index, SQLiteDecisionRecords(tmp_path), clock, cold_tracker, strategy)
    assert restored.restore(clock.value.date()).record == result.record
    restored_receipt = _receipt(cold_tracker, "formal_restore")
    assert restored_receipt.output_version == result.record.decision.version
    assert {item.operation for item in cold_tracker.snapshots()} == {"formal_lookup", "formal_restore"}
    assert (
        restored.freeze_close_fallback(
            replace(current, sequence=9), recovery_path="close_rebuild", official_close_version="official-close:late"
        ).record
        == result.record
    )
    assert records.load(strategy, clock.value.date()) == result.record


@pytest.mark.parametrize("strategy", (Strategy.TOMORROW, Strategy.D25))
def test_write_failure_retry_keeps_sealed_content_and_exposes_real_result(tmp_path, monkeypatch, strategy) -> None:
    clock = _Clock(_at(15, 0))
    tracker = PublicationIoTracker(now=clock.now, monotonic=_Monotonic())
    index = UnifiedDecisionIndex()
    current = replace(decision(strategy), observed_at=_at(14, 59, 35))
    assert index.publish(current, expected_version=None).accepted
    records = SQLiteDecisionRecords(tmp_path)
    records.initialize()
    commit = records.commit
    attempted = []

    def fail_once(record):
        attempted.append(record)
        if len(attempted) == 1:
            raise DecisionRecordUnavailableError("controlled_write_failure")
        commit(record)

    monkeypatch.setattr(records, "commit", fail_once)
    freezer = _freezer(index, records, clock, tracker, strategy)
    assert freezer.freeze_scheduled().status == "persistence_failed"
    failure = _receipt(tracker, "formal_write")
    assert failure.failed_count == 1 and failure.rejected_count == failure.output_count == 0
    assert failure.source_health.state is SourceHealthState.UNAVAILABLE
    assert index.snapshot(strategy).current == current
    assert not index.publish(replace(current, sequence=20), expected_version=current.version).accepted
    result = freezer.freeze_scheduled()
    assert result.status == "frozen" and result.record is not None
    assert attempted[0] == attempted[1] == result.record
    assert failure.state is StageState.FAILED  # Earlier immutable receipt is unchanged.
    payload = publication_io_payload(_receipt(tracker, "formal_write"))
    assert payload["count_unit"] == "operation" and payload["output_count"] == 1
    assert "rejection_rate" not in payload and "items" not in payload
    _assert_formal_current_and_history(index, records, clock, result.record)


def test_cleanup_failure_is_visible_without_revoking_formal_record(tmp_path, monkeypatch) -> None:
    clock = _Clock(_at(14, 59, 40))
    tracker = PublicationIoTracker(now=clock.now, monotonic=_Monotonic())
    index = UnifiedDecisionIndex()
    current = replace(decision(), observed_at=_at(14, 59, 35))
    assert index.publish(current, expected_version=None).accepted
    records = SQLiteDecisionRecords(tmp_path)
    records.initialize()
    freezer = _freezer(index, records, clock, tracker, Strategy.TOMORROW)
    assert freezer.capture_checkpoint().status == "checkpoint_saved"

    def fail_cleanup(*_args, **_kwargs):
        raise OSError("controlled_cleanup_failure")

    monkeypatch.setattr(records, "consume_checkpoint", fail_cleanup)
    clock.value = _at(15, 0)
    result = freezer.freeze_scheduled()
    assert result.status == "frozen" and result.record is not None
    assert _receipt(tracker, "checkpoint_consume").failed_count == 1
    assert _receipt(tracker, "formal_publish").output_count == 1
    assert freezer.freeze_scheduled().record == result.record
    _assert_formal_current_and_history(index, records, clock, result.record)


def test_late_completion_and_older_cycle_cannot_replace_new_io_and_age_is_preserved() -> None:
    clock = _Clock(_at(14, 50))
    tracker = PublicationIoTracker(now=clock.now, monotonic=_Monotonic())
    old = PublicationIoTarget.decision(decision())
    new = PublicationIoTarget.decision(replace(decision(), sequence=3))
    first = tracker.begin("current_publish", old)
    second = tracker.begin("current_publish", new)
    tracker.finish(*second, "current_publish", new, PublicationIoCompletion(), error=None)
    tracker.finish(*first, "current_publish", old, PublicationIoCompletion(), error="io_oserror")
    with observe_publication_io(tracker, "current_publish", old):
        pass
    assert _receipt(tracker, "current_publish").target == new
    clock.value += timedelta(seconds=20)
    with pytest.raises(OSError), observe_publication_io(tracker, "current_publish", new):
        raise OSError("secret external payload must not escape")
    receipt = _receipt(tracker, "current_publish")
    assert receipt.source_health.age_seconds == 20.0
    assert receipt.reasons[0].code == "io_oserror"
    assert "secret" not in str(publication_io_payload(receipt))
    with pytest.raises(FrozenInstanceError):
        receipt.output_count = 1
    with pytest.raises(ValueError):
        PublicationIoTarget(Strategy.LONG, clock.value.date(), None, 0)


def test_observation_clock_failure_cannot_block_freeze(tmp_path) -> None:
    clock = _Clock(_at(15, 0))

    def broken_clock():
        raise RuntimeError("telemetry unavailable")

    tracker = PublicationIoTracker(now=broken_clock, monotonic=_Monotonic())
    index = UnifiedDecisionIndex()
    current = replace(decision(), observed_at=_at(14, 59, 35))
    assert index.publish(current, expected_version=None).accepted
    records = SQLiteDecisionRecords(tmp_path)
    records.initialize()
    result = _freezer(index, records, clock, tracker, Strategy.TOMORROW).freeze_scheduled()
    assert result.status == "frozen" and result.record is not None
    assert records.load(Strategy.TOMORROW, clock.value.date()) == result.record
    assert tracker.snapshots() == ()


def test_cold_lookup_after_success_reports_latest_failure(tmp_path, monkeypatch) -> None:
    clock = _Clock(_at(15, 0))
    tracker = PublicationIoTracker(now=clock.now, monotonic=_Monotonic())
    index = UnifiedDecisionIndex()
    current = replace(decision(), observed_at=_at(14, 59, 35))
    assert index.publish(current, expected_version=None).accepted
    records = SQLiteDecisionRecords(tmp_path)
    records.initialize()
    result = _freezer(index, records, clock, tracker, Strategy.TOMORROW).freeze_scheduled()
    assert result.status == "frozen"
    cold = _freezer(UnifiedDecisionIndex(), records, clock, tracker, Strategy.TOMORROW)
    assert cold.restore(clock.value.date()).status == "already_frozen"
    previous = _receipt(tracker, "formal_lookup")

    def fail_lookup(*_args):
        raise DecisionRecordUnavailableError("controlled_lookup_failure")

    monkeypatch.setattr(records, "load", fail_lookup)
    cold_again = _freezer(UnifiedDecisionIndex(), records, clock, tracker, Strategy.TOMORROW)
    assert cold_again.restore(clock.value.date()).status == "persistence_failed"
    latest = _receipt(tracker, "formal_lookup")
    assert latest.attempt_id > previous.attempt_id
    assert latest.failed_count == 1 and latest.target.decision_version is None
    assert latest.source_health.latest_success_at == previous.source_health.latest_success_at


def test_ineligible_checkpoint_remains_pending_without_business_rejection(tmp_path) -> None:
    clock = _Clock(_at(15, 0))
    tracker = PublicationIoTracker(now=clock.now, monotonic=_Monotonic())
    records = SQLiteDecisionRecords(tmp_path)
    records.initialize()
    checkpoint = DecisionCheckpoint(formal_scored_decision(decision()), _at(15, 0))
    records.save_checkpoint(checkpoint)
    freezer = _freezer(UnifiedDecisionIndex(), records, clock, tracker, Strategy.TOMORROW)
    assert freezer.freeze_scheduled().status == "no_eligible_decision"
    receipt = _receipt(tracker, "checkpoint_lookup")
    assert receipt.pending_count == 1 and receipt.output_count == receipt.rejected_count == receipt.failed_count == 0
    assert receipt.reasons[0].code == "checkpoint_ineligible"
    assert receipt.source_health.state is SourceHealthState.READY


def test_status_clock_failure_retains_receipt_with_unknown_age() -> None:
    clock = _Clock(_at(14, 50))
    available = True

    def now():
        if not available:
            raise RuntimeError("clock unavailable")
        return clock.now()

    tracker = PublicationIoTracker(now=now, monotonic=_Monotonic())
    with observe_publication_io(tracker, "current_publish", PublicationIoTarget.decision(decision())):
        pass
    before = tracker.snapshots()[0]
    available = False
    after = tracker.snapshots()[0]
    assert after.attempt_id == before.attempt_id and after.output_count == 1
    assert after.source_health.age_seconds is None
