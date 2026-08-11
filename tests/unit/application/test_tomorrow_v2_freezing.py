from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from tests.unit.domain.test_decision_identity import decision
from trader.application.decision_core import UnifiedDecisionIndex
from trader.application.tomorrow_v2_freezing import (
    TomorrowV2FreezeCoordinator,
    V2DecisionRuntimeIdentity,
)
from trader.domain.recommendation.decision_identity import ScoredDecision
from trader.domain.recommendation.models import Strategy
from trader.infra.persistence.decision_records import SQLiteDecisionRecordRepository

SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass
class _Clock:
    value: datetime

    def now(self) -> datetime:
        return self.value


def _coordinator(
    index: UnifiedDecisionIndex,
    repository: SQLiteDecisionRecordRepository,
    clock: _Clock,
    strategy: Strategy = Strategy.TOMORROW,
) -> TomorrowV2FreezeCoordinator:
    return TomorrowV2FreezeCoordinator(
        index,
        repository,
        clock,
        runtime_identity=V2DecisionRuntimeIdentity("config-v1", "strategy-v1", "fusion-v1"),
        strategy=strategy,
    )


def _at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 8, 11, hour, minute, second, tzinfo=SHANGHAI)


def _publish(index: UnifiedDecisionIndex, value: ScoredDecision) -> None:
    assert index.publish(value, expected_version=None).accepted


def test_checkpoint_recovers_same_v2_identity_after_restart(tmp_path: Path) -> None:
    repository = SQLiteDecisionRecordRepository(tmp_path)
    repository.initialize()
    before = UnifiedDecisionIndex()
    current = replace(decision(), observed_at=_at(14, 49, 35))
    _publish(before, current)
    clock = _Clock(_at(14, 49, 40))

    assert _coordinator(before, repository, clock).capture_checkpoint().status == "checkpoint_saved"

    restored = UnifiedDecisionIndex()
    clock.value = _at(14, 50)
    result = _coordinator(restored, repository, clock, strategy=Strategy.TOMORROW).freeze_scheduled()

    assert result.status == "frozen"
    assert result.record is not None
    assert result.record.commit_kind == "checkpoint_recovery"
    restored_current = restored.snapshot(Strategy.TOMORROW).current
    assert isinstance(restored_current, ScoredDecision)
    assert result.record.decision.content_hash == restored_current.content_hash


def test_freeze_is_idempotent_non_overwritable_and_accepts_empty_formal_result(tmp_path: Path) -> None:
    repository = SQLiteDecisionRecordRepository(tmp_path)
    repository.initialize()
    index = UnifiedDecisionIndex()
    empty = replace(decision(), observed_at=_at(14, 49, 50), items=())
    _publish(index, empty)
    clock = _Clock(_at(14, 50))
    coordinator = _coordinator(index, repository, clock)

    first = coordinator.freeze_scheduled()
    second = coordinator.freeze_scheduled()

    assert first.status == "frozen"
    assert first.record is not None and first.record.decision.items == ()
    assert second.status == "already_frozen"
    assert second.version == first.version
    late = replace(empty, sequence=3, observed_at=_at(14, 50, 1))
    assert index.publish(late, expected_version=empty.version).reason == "freeze_sealed"


def test_close_fallback_requires_official_close_and_never_overwrites(tmp_path: Path) -> None:
    repository = SQLiteDecisionRecordRepository(tmp_path)
    repository.initialize()
    index = UnifiedDecisionIndex()
    current = replace(decision(), observed_at=_at(14, 49, 50))
    _publish(index, current)
    coordinator = _coordinator(index, repository, _Clock(_at(15, 0)))

    invalid = coordinator.freeze_close_fallback(
        current,
        recovery_path="current",
        official_close_version="candidate-v1",
    )
    frozen = coordinator.freeze_close_fallback(
        current,
        recovery_path="current",
        official_close_version="official-close:20260811",
    )
    duplicate = coordinator.freeze_close_fallback(
        current,
        recovery_path="current",
        official_close_version="official-close:20260811",
    )

    assert invalid.status == "invalid_official_close"
    assert frozen.status == "frozen"
    assert frozen.record is not None
    assert frozen.record.decision.degraded_reasons == (
        "close_fallback",
        "local_only",
        "official_close",
    )
    assert dict(frozen.record.decision.input_versions)["official_close"] == "official-close:20260811"
    assert duplicate.status == "already_frozen"


def test_d25_checkpoint_and_close_recovery_use_d25_path(tmp_path: Path) -> None:
    repository = SQLiteDecisionRecordRepository(tmp_path)
    repository.initialize()
    index = UnifiedDecisionIndex()
    current = replace(
        decision(Strategy.D25),
        observed_at=_at(14, 49, 35),
    )
    _publish(index, current)
    coordinator = _coordinator(index, repository, _Clock(_at(14, 49, 40)), strategy=Strategy.D25)
    assert coordinator.capture_checkpoint().status == "checkpoint_saved"

    clock = _Clock(_at(14, 50))
    coordinator = _coordinator(index, repository, clock, strategy=Strategy.D25)
    result = coordinator.freeze_scheduled()

    assert result.status == "frozen"
    assert result.record is not None
    assert result.record.decision.strategy is Strategy.D25
    restored = UnifiedDecisionIndex()
    assert (
        _coordinator(restored, repository, clock, strategy=Strategy.D25).restore(_at(14, 50).date()).status
        == "already_frozen"
    )


def test_d25_freeze_close_fallback_persists_d25_formal_record(tmp_path: Path) -> None:
    repository = SQLiteDecisionRecordRepository(tmp_path)
    repository.initialize()
    index = UnifiedDecisionIndex()
    current = replace(
        decision(Strategy.D25),
        observed_at=_at(14, 49, 50),
        sequence=2,
    )
    _publish(index, current)
    coordinator = _coordinator(index, repository, _Clock(_at(15, 0, 1)), strategy=Strategy.D25)

    first = coordinator.freeze_close_fallback(
        current,
        recovery_path="current",
        official_close_version="official-close:20260811",
    )
    duplicate = coordinator.freeze_close_fallback(
        current,
        recovery_path="current",
        official_close_version="official-close:20260811",
    )

    assert first.status == "frozen"
    assert first.record is not None
    assert first.record.decision.strategy is Strategy.D25
    assert dict(first.record.decision.input_versions)["official_close"] == "official-close:20260811"
    assert duplicate.status == "already_frozen"


def test_d25_empty_formal_and_tomorrow_formal_are_isolated_by_strategy(tmp_path: Path) -> None:
    repository = SQLiteDecisionRecordRepository(tmp_path)
    repository.initialize()
    index = UnifiedDecisionIndex()
    tomorrow = replace(decision(Strategy.TOMORROW), observed_at=_at(14, 49, 50))
    d25 = replace(decision(Strategy.D25), observed_at=_at(14, 49, 49), items=())
    _publish(index, tomorrow)
    _publish(index, d25)
    clock = _Clock(_at(14, 50))

    tomorrow_result = _coordinator(index, repository, clock, Strategy.TOMORROW).freeze_scheduled()
    d25_result = _coordinator(index, repository, clock, Strategy.D25).freeze_scheduled()

    assert tomorrow_result.status == "frozen"
    assert d25_result.status == "frozen"
    assert d25_result.record is not None and d25_result.record.decision.items == ()
    assert repository.load(Strategy.TOMORROW, d25.trade_date) == tomorrow_result.record
    assert repository.load(Strategy.D25, d25.trade_date) == d25_result.record


def test_d25_close_fallback_rejects_pending_scheduled_seal(tmp_path: Path) -> None:
    repository = SQLiteDecisionRecordRepository(tmp_path)
    repository.initialize()
    index = UnifiedDecisionIndex()
    current = replace(decision(Strategy.D25), observed_at=_at(14, 49, 50))
    _publish(index, current)
    assert index.seal_for_freeze(Strategy.D25, boundary_at=_at(14, 50)).accepted
    coordinator = _coordinator(index, repository, _Clock(_at(15, 0, 1)), Strategy.D25)

    result = coordinator.freeze_close_fallback(
        current,
        recovery_path="current",
        official_close_version="official-close:20260811",
    )

    assert result.status == "scheduled_freeze_pending"
    assert repository.load(Strategy.D25, current.trade_date) is None
