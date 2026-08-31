from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from tests.unit.domain.test_decision_identity import decision
from trader.application.decisions.decision_core import UnifiedDecisionIndex
from trader.application.recommendation.scored_v2_freezing import V2DecisionRuntimeIdentity
from trader.application.recommendation.today_v2_freezing import TodayV2FreezeCoordinator
from trader.domain.recommendation.decision_identity import ScoredDecision
from trader.domain.recommendation.models import Strategy
from trader.infra.persistence.decision_records import SQLiteDecisionRecordRepository

SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass
class _Clock:
    value: datetime

    def now(self) -> datetime:
        return self.value


def _at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 8, 11, hour, minute, second, tzinfo=SHANGHAI)


def _today(at: datetime, *, sequence: int = 1) -> ScoredDecision:
    fixture = decision(Strategy.TODAY, sequence=sequence)
    quote = fixture.items[0].quote
    assert quote is not None
    return replace(
        fixture,
        observed_at=at,
        items=(replace(fixture.items[0], quote=replace(quote, source_time=at)),),
    )


def _coordinator(
    index: UnifiedDecisionIndex,
    repository: SQLiteDecisionRecordRepository,
    clock: _Clock,
) -> TodayV2FreezeCoordinator:
    return TodayV2FreezeCoordinator(
        index,
        repository,
        clock,
        runtime_identity=V2DecisionRuntimeIdentity("config-v1", "strategy-v1", "fusion-v1"),
    )


def test_111959_is_frozen_at_112000_and_retry_never_changes_the_record(tmp_path: Path) -> None:
    repository = SQLiteDecisionRecordRepository(tmp_path)
    repository.initialize()
    index = UnifiedDecisionIndex()
    current = _today(_at(11, 19, 59))
    assert index.publish(current, expected_version=None).accepted
    clock = _Clock(_at(11, 20))
    coordinator = _coordinator(index, repository, clock)

    first = coordinator.freeze_scheduled()
    second = coordinator.freeze_scheduled()

    assert first.status == "frozen"
    assert first.record is not None and first.record.commit_kind == "scheduled"
    assert second.status == "already_frozen" and second.version == first.version
    late = _today(_at(11, 20, 1), sequence=3)
    assert index.publish(late, expected_version=first.record.decision.version).reason == "freeze_sealed"
    assert repository.load_checkpoint(Strategy.TODAY, current.trade_date) is None


def test_starting_at_boundary_without_a_formal_record_is_permanently_missed(tmp_path: Path) -> None:
    repository = SQLiteDecisionRecordRepository(tmp_path)
    repository.initialize()
    index = UnifiedDecisionIndex()
    clock = _Clock(_at(11, 20))
    coordinator = _coordinator(index, repository, clock)

    initialized = coordinator.initialize()
    clock.value = _at(15, 0)
    retried = coordinator.freeze_scheduled()

    assert initialized.status == "missed_freeze"
    assert retried.status == "missed_freeze"
    assert repository.load(Strategy.TODAY, clock.value.date()) is None
    assert index.publish(_today(_at(15, 0)), expected_version=None).reason == "freeze_closed"


def test_scheduler_delay_within_boundary_second_freezes_the_latest_eligible_current(tmp_path: Path) -> None:
    repository = SQLiteDecisionRecordRepository(tmp_path)
    repository.initialize()
    index = UnifiedDecisionIndex()
    current = _today(_at(11, 19, 59))
    assert index.publish(current, expected_version=None).accepted
    coordinator = _coordinator(index, repository, _Clock(_at(11, 20, 0).replace(microsecond=1)))

    result = coordinator.freeze_scheduled()

    assert result.status == "frozen"
    assert result.record is not None and result.record.decision.observed_at == current.observed_at
    assert repository.load(Strategy.TODAY, current.trade_date) == result.record


def test_first_freeze_attempt_after_boundary_second_cannot_backfill_today(tmp_path: Path) -> None:
    repository = SQLiteDecisionRecordRepository(tmp_path)
    repository.initialize()
    index = UnifiedDecisionIndex()
    current = _today(_at(11, 19, 59))
    assert index.publish(current, expected_version=None).accepted
    coordinator = _coordinator(index, repository, _Clock(_at(11, 20, 1)))

    result = coordinator.freeze_scheduled()

    assert result.status == "missed_freeze"
    assert repository.load(Strategy.TODAY, current.trade_date) is None
    assert index.snapshot(Strategy.TODAY).current is None
    assert index.publish(_today(_at(11, 20, 1), sequence=3), expected_version=current.version).reason == "freeze_closed"


def test_persistence_failure_retries_the_same_sealed_version(tmp_path: Path) -> None:
    failed = False

    def fail_once(point: str) -> None:
        nonlocal failed
        if point == "manifest_staged" and not failed:
            failed = True
            raise OSError("injected")

    repository = SQLiteDecisionRecordRepository(tmp_path, fault_injector=fail_once)
    repository.initialize()
    index = UnifiedDecisionIndex()
    current = _today(_at(11, 19, 59))
    assert index.publish(current, expected_version=None).accepted
    coordinator = _coordinator(index, repository, _Clock(_at(11, 20)))

    first = coordinator.freeze_scheduled()
    second = coordinator.freeze_scheduled()

    assert first.status == "persistence_failed"
    assert second.status == "frozen"
    assert first.version == second.version


def test_boundary_read_failure_still_closes_and_seals_the_retry_candidate(tmp_path: Path) -> None:
    repository = SQLiteDecisionRecordRepository(tmp_path)
    repository.initialize()

    class FailFirstLoad:
        def __init__(self) -> None:
            self.failed = False

        def load(self, strategy, trade_date):
            if not self.failed:
                self.failed = True
                raise OSError("injected")
            return repository.load(strategy, trade_date)

        def __getattr__(self, name):
            return getattr(repository, name)

    index = UnifiedDecisionIndex()
    current = _today(_at(11, 19, 59))
    assert index.publish(current, expected_version=None).accepted
    coordinator = TodayV2FreezeCoordinator(
        index,
        FailFirstLoad(),
        _Clock(_at(11, 20)),
        runtime_identity=V2DecisionRuntimeIdentity("config-v1", "strategy-v1", "fusion-v1"),
    )

    first = coordinator.freeze_scheduled()
    late = index.publish(_today(_at(11, 20, 1), sequence=3), expected_version=current.version)
    second = coordinator.freeze_scheduled()

    assert first.status == "persistence_failed"
    assert late.reason == "freeze_sealed"
    assert second.status == "frozen" and second.version == first.version


def test_existing_formal_record_is_restored_after_boundary_instead_of_marked_missed(tmp_path: Path) -> None:
    repository = SQLiteDecisionRecordRepository(tmp_path)
    repository.initialize()
    original_index = UnifiedDecisionIndex()
    current = _today(_at(11, 19, 59))
    assert original_index.publish(current, expected_version=None).accepted
    first = _coordinator(original_index, repository, _Clock(_at(11, 20))).freeze_scheduled()
    assert first.record is not None

    restored_index = UnifiedDecisionIndex()
    restored = _coordinator(restored_index, repository, _Clock(_at(12, 0))).initialize()

    assert restored.status == "already_frozen"
    assert restored.record == first.record
    assert restored_index.snapshot(Strategy.TODAY).formal == first.record
