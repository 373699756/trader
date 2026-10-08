from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tests.unit.domain.test_decision_identity import decision
from trader.infra.settings import load_strategy_settings
from trader.recommendation.application.pipeline.freeze_publish.draft_index import UnifiedDecisionDraftIndex
from trader.recommendation.application.pipeline.freeze_publish.freeze_coordinator import (
    DecisionRuntimeIdentity,
    ScoredFreezeCoordinator,
)
from trader.recommendation.application.pipeline.freeze_publish.read_only_queries import UnifiedDecisionQueries
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import UnifiedDecisionIndex
from trader.recommendation.domain.publication.decision_identity import CommittedDecisionRecord, ScoredDecision
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.infra.persistence.decision_records import SQLiteDecisionRecordRepository

SHANGHAI = ZoneInfo("Asia/Shanghai")
PROJECT_ROOT = Path(__file__).resolve().parents[3]


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
) -> ScoredFreezeCoordinator:
    return ScoredFreezeCoordinator(
        index,
        repository,
        clock,
        runtime_identity=DecisionRuntimeIdentity("config-current", "strategy-current", "fusion-current"),
        strategy=strategy,
    )


def _at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 8, 11, hour, minute, second, tzinfo=SHANGHAI)


def _publish(index: UnifiedDecisionIndex, value: ScoredDecision) -> None:
    assert index.publish(value, expected_version=None).accepted


@pytest.mark.parametrize("strategy", (Strategy.TOMORROW, Strategy.D25))
def test_checkpoint_recovers_same_decision_identity_after_restart(tmp_path: Path, strategy: Strategy) -> None:
    repository = SQLiteDecisionRecordRepository(tmp_path)
    repository.initialize()
    before = UnifiedDecisionIndex()
    current = replace(decision(strategy), observed_at=_at(14, 59, 35))
    _publish(before, current)
    clock = _Clock(_at(14, 59, 40))

    assert _coordinator(before, repository, clock, strategy).capture_checkpoint().status == "checkpoint_saved"

    repository = SQLiteDecisionRecordRepository(tmp_path)
    restored = UnifiedDecisionIndex()
    clock.value = _at(15, 0)
    result = _coordinator(restored, repository, clock, strategy).freeze_scheduled()

    assert result.status == "frozen"
    assert result.record is not None
    assert result.record.commit_kind == "checkpoint_recovery"
    restored_current = restored.snapshot(strategy).current
    assert isinstance(restored_current, ScoredDecision)
    assert result.record.decision.content_hash == restored_current.content_hash
    _assert_formal_current_and_history(restored, repository, clock, result.record)


def _assert_formal_current_and_history(
    index: UnifiedDecisionIndex,
    repository: SQLiteDecisionRecordRepository,
    clock: _Clock,
    record: CommittedDecisionRecord,
) -> None:
    queries = UnifiedDecisionQueries(index, UnifiedDecisionDraftIndex(), repository, clock)
    for at in (_at(15, 0), _at(15, 0, 1), _at(15, 5)):
        if at < clock.value:
            continue
        clock.value = at
        current = queries.current(record.strategy)
        history = queries.history(record.strategy, record.trade_date)
        for view in (current, history):
            assert view.status == "ready"
            assert view.frozen is True
            assert view.frozen_at == record.committed_at
            assert view.freeze_kind == record.commit_kind
            assert view.decision_version == record.decision.version
            assert view.content_hash == record.decision.content_hash
            assert view.input_versions == record.decision.input_versions
            assert view.draft is None
        assert current.items == history.items
        assert current.top_scores == history.top_scores
        assert repository.load(record.strategy, record.trade_date) == record


@pytest.mark.parametrize("strategy", (Strategy.TOMORROW, Strategy.D25))
@pytest.mark.parametrize("cold_read", (False, True), ids=("hot", "cold"))
def test_scheduled_close_boundary_and_formal_reads(tmp_path: Path, strategy: Strategy, cold_read: bool) -> None:
    repository = SQLiteDecisionRecordRepository(tmp_path)
    repository.initialize()
    index = UnifiedDecisionIndex()
    drafts = UnifiedDecisionDraftIndex()
    clock = _Clock(_at(9, 25))
    coordinator = _coordinator(index, repository, clock, strategy)
    queries = UnifiedDecisionQueries(index, drafts, repository, clock)
    previous_version: str | None = None
    for sequence, at in enumerate((_at(9, 25), _at(14, 40), _at(14, 50), _at(14, 59, 59)), start=1):
        clock.value = at
        base = decision(strategy, sequence=sequence)
        item = base.items[0]
        assert item.quote is not None
        current = replace(base, observed_at=at, items=(replace(item, quote=replace(item.quote, source_time=at)),))
        assert index.publish(current, expected_version=previous_version).accepted
        previous_version = current.version
        assert drafts.publish(current).accepted
        assert coordinator.freeze_scheduled().status == "before_freeze"
        assert repository.load(strategy, current.trade_date) is None
        view = queries.current(strategy)
        assert view.status == "ready"
        assert view.frozen is False
        assert view.decision_version == current.version

    clock.value = _at(15, 0)
    missing_formal = queries.current(strategy)
    assert missing_formal.status == "not_ready"
    assert missing_formal.items == ()
    assert missing_formal.draft is None
    assert missing_formal.etag is None

    result = coordinator.freeze_scheduled()
    assert result.status == "frozen"
    assert result.record is not None
    assert result.record.commit_kind == "scheduled"
    assert result.record.committed_at == _at(15, 0)
    assert result.record.decision.content_hash == current.content_hash
    if cold_read:
        repository = SQLiteDecisionRecordRepository(tmp_path)
        index = UnifiedDecisionIndex()
        coordinator = _coordinator(index, repository, clock, strategy)
        assert UnifiedDecisionQueries(index, drafts, repository, clock).current(strategy).status == "not_ready"
        restored = coordinator.restore(current.trade_date)
        assert restored.status == "already_frozen"
        assert restored.record == result.record
    _assert_formal_current_and_history(index, repository, clock, result.record)

    late = replace(current, sequence=99, observed_at=_at(15, 5), degraded_reasons=("late_result",))
    assert not index.publish(late, expected_version=current.version).accepted
    assert coordinator.freeze_scheduled().record == result.record
    assert (
        coordinator.freeze_close_fallback(
            late, recovery_path="close_rebuild", official_close_version="official-close:20260811"
        ).record
        == result.record
    )
    assert repository.load(strategy, current.trade_date) == result.record


@pytest.mark.parametrize("strategy", (Strategy.TOMORROW, Strategy.D25))
@pytest.mark.parametrize("cold_start", (False, True), ids=("hot-current", "cold-rebuild"))
@pytest.mark.parametrize("recovery_minute", (0, 5), ids=("at-close", "after-close"))
def test_close_fallback_admission_and_recovered_formal_reads(
    tmp_path: Path, strategy: Strategy, cold_start: bool, recovery_minute: int
) -> None:
    repository = SQLiteDecisionRecordRepository(tmp_path)
    repository.initialize()
    index = UnifiedDecisionIndex()
    current = replace(decision(strategy), observed_at=_at(14, 59, 59))
    if not cold_start:
        _publish(index, current)
    clock = _Clock(_at(14, 59, 59))
    coordinator = _coordinator(index, repository, clock, strategy)
    recovery_path = "close_rebuild" if cold_start else "current"
    before = coordinator.freeze_close_fallback(
        current, recovery_path=recovery_path, official_close_version="official-close:20260811"
    )
    assert before.status == "before_close_recovery"
    assert repository.load(strategy, current.trade_date) is None

    clock.value = _at(15, recovery_minute)
    invalid = coordinator.freeze_close_fallback(
        current, recovery_path=recovery_path, official_close_version="candidate-initial"
    )
    assert invalid.status == "invalid_official_close"
    assert repository.load(strategy, current.trade_date) is None
    result = coordinator.freeze_close_fallback(
        current, recovery_path=recovery_path, official_close_version="official-close:20260811"
    )
    assert result.status == "frozen"
    assert result.record is not None
    assert result.record.commit_kind == "close_fallback"
    assert dict(result.record.decision.input_versions)["official_close"] == "official-close:20260811"
    _assert_formal_current_and_history(index, repository, clock, result.record)

    repository = SQLiteDecisionRecordRepository(tmp_path)
    restored = UnifiedDecisionIndex()
    coordinator = _coordinator(restored, repository, clock, strategy)
    duplicate = coordinator.freeze_close_fallback(
        replace(current, sequence=99, observed_at=clock.value, degraded_reasons=("late_result",)),
        recovery_path="close_rebuild",
        official_close_version="official-close:20260811-later",
    )
    assert duplicate.status == "already_frozen"
    assert duplicate.record == result.record
    _assert_formal_current_and_history(restored, repository, clock, result.record)


def test_weight_configuration_hash_participates_in_freeze_runtime_identity(tmp_path: Path) -> None:
    strategy_path = PROJECT_ROOT / "config" / "strategy.json"
    baseline = load_strategy_settings(strategy_path)
    raw = json.loads(strategy_path.read_text(encoding="utf-8"))
    weights = raw["local_component_weights"]["tomorrow"]["trend"]
    weights["ma20_60_position"] -= 0.01
    weights["ma_slope"] += 0.01
    changed_path = tmp_path / "strategy.json"
    changed_path.write_text(json.dumps(raw), encoding="utf-8")
    changed = load_strategy_settings(changed_path)
    current = replace(decision(), strategy_version=baseline.strategy_version)

    baseline_identity = DecisionRuntimeIdentity(
        current.config_version,
        baseline.strategy_version,
        current.fusion_version,
    )
    changed_identity = replace(baseline_identity, strategy_version=changed.strategy_version)

    assert baseline.strategy_version != changed.strategy_version
    assert baseline_identity.matches(current)
    assert not changed_identity.matches(current)


def test_freeze_is_idempotent_non_overwritable_and_accepts_empty_formal_result(tmp_path: Path) -> None:
    repository = SQLiteDecisionRecordRepository(tmp_path)
    repository.initialize()
    index = UnifiedDecisionIndex()
    empty = replace(decision(), observed_at=_at(14, 59, 50), items=())
    _publish(index, empty)
    clock = _Clock(_at(15, 0))
    coordinator = _coordinator(index, repository, clock)

    first = coordinator.freeze_scheduled()
    second = coordinator.freeze_scheduled()

    assert first.status == "frozen"
    assert first.record is not None and first.record.decision.items == ()
    assert second.status == "already_frozen"
    assert second.version == first.version
    late = replace(empty, sequence=3, observed_at=_at(15, 0, 1))
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
        official_close_version="candidate-initial",
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
        observed_at=_at(14, 59, 35),
    )
    _publish(index, current)
    coordinator = _coordinator(index, repository, _Clock(_at(14, 59, 40)), strategy=Strategy.D25)
    assert coordinator.capture_checkpoint().status == "checkpoint_saved"

    clock = _Clock(_at(15, 0))
    coordinator = _coordinator(index, repository, clock, strategy=Strategy.D25)
    result = coordinator.freeze_scheduled()

    assert result.status == "frozen"
    assert result.record is not None
    assert result.record.decision.strategy is Strategy.D25
    restored = UnifiedDecisionIndex()
    assert (
        _coordinator(restored, repository, clock, strategy=Strategy.D25).restore(_at(15, 0).date()).status
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
    tomorrow = replace(decision(Strategy.TOMORROW), observed_at=_at(14, 59, 50))
    d25 = replace(decision(Strategy.D25), observed_at=_at(14, 59, 49), items=())
    _publish(index, tomorrow)
    _publish(index, d25)
    clock = _Clock(_at(15, 0))

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
    assert index.seal_for_freeze(Strategy.D25, boundary_at=_at(15, 0)).accepted
    coordinator = _coordinator(index, repository, _Clock(_at(15, 0, 1)), Strategy.D25)

    result = coordinator.freeze_close_fallback(
        current,
        recovery_path="current",
        official_close_version="official-close:20260811",
    )

    assert result.status == "scheduled_freeze_pending"
    assert repository.load(Strategy.D25, current.trade_date) is None
