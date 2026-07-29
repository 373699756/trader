from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from tests.unit.domain.test_tomorrow_fusion import _evaluation, _request, _selection
from trader.application.current_decisions import CurrentDecisionIndex
from trader.application.ports.decision_freezes import DecisionFreezeUnavailableError
from trader.application.tomorrow_freezing import (
    DecisionRuntimeIdentity,
    TomorrowFreezeCoordinator,
)
from trader.domain.recommendation.tomorrow_freeze import (
    TomorrowDecisionFreeze,
    TomorrowFreezeCheckpoint,
    build_decision_anchors,
)
from trader.domain.recommendation.tomorrow_fusion import build_tomorrow_decision_epoch

SHANGHAI = ZoneInfo("Asia/Shanghai")
BOUNDARY = datetime(2026, 7, 28, 14, 50, tzinfo=SHANGHAI)
CLOSE = datetime(2026, 7, 28, 15, 0, tzinfo=SHANGHAI)


class _Clock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def now(self) -> datetime:
        return self.current


class _Repository:
    def __init__(self) -> None:
        self.checkpoint: TomorrowFreezeCheckpoint | None = None
        self.frozen: TomorrowDecisionFreeze | None = None
        self.fail_commit = False
        self.fail_checkpoint_load = False
        self.index_during_commit: CurrentDecisionIndex | None = None
        self.was_frozen_during_commit: bool | None = None

    def save_checkpoint(self, checkpoint: TomorrowFreezeCheckpoint) -> None:
        self.checkpoint = checkpoint

    def load_checkpoint(self, trade_date) -> TomorrowFreezeCheckpoint | None:
        if self.fail_checkpoint_load:
            raise DecisionFreezeUnavailableError("injected checkpoint load")
        return self.checkpoint if self.checkpoint is not None and self.checkpoint.trade_date == trade_date else None

    def consume_checkpoint(self, checkpoint_version: str, *, consumed_at: datetime) -> None:
        if self.checkpoint is not None and self.checkpoint.version == checkpoint_version:
            self.checkpoint = None

    def commit_freeze(self, frozen: TomorrowDecisionFreeze) -> None:
        if self.index_during_commit is not None:
            self.was_frozen_during_commit = self.index_during_commit.frozen() is not None
        if self.fail_commit:
            raise DecisionFreezeUnavailableError("injected")
        self.frozen = frozen

    def load_frozen(self, trade_date) -> TomorrowDecisionFreeze | None:
        return self.frozen if self.frozen is not None and self.frozen.trade_date == trade_date else None


def test_checkpoint_then_scheduled_freeze_persists_before_index_commit() -> None:
    clock = _Clock(BOUNDARY - timedelta(seconds=10))
    index = CurrentDecisionIndex()
    repository = _Repository()
    repository.index_during_commit = index
    decision = _decision(1, clock.current)
    index.publish(decision, expected_current_version=None)
    coordinator = _coordinator(index, repository, clock)

    checkpoint = coordinator.capture_checkpoint()
    clock.current = BOUNDARY
    result = coordinator.freeze_scheduled()

    assert checkpoint.status == "checkpoint_saved"
    assert result.status == "frozen"
    assert result.frozen is not None
    assert result.frozen.freeze_kind == "scheduled"
    assert repository.was_frozen_during_commit is False
    assert index.frozen() == result.frozen
    assert repository.checkpoint is None


def test_checkpoint_and_freeze_keep_only_formal_tomorrow_entries() -> None:
    clock = _Clock(BOUNDARY - timedelta(seconds=10))
    index = CurrentDecisionIndex()
    repository = _Repository()
    evaluations = (
        _evaluation(1, local_score=90.0),
        _evaluation(2, local_score=74.0),
    )
    decision = build_tomorrow_decision_epoch(
        replace(
            _request(_selection(evaluations)),
            sequence=1,
            observed_at=clock.current,
        )
    )
    index.publish(decision, expected_current_version=None)
    coordinator = _coordinator(index, repository, clock)

    checkpoint = coordinator.capture_checkpoint()
    assert checkpoint.status == "checkpoint_saved"
    assert repository.checkpoint is not None
    assert [item.code for item in repository.checkpoint.decision.entries] == ["600001"]

    clock.current = BOUNDARY
    frozen = coordinator.freeze_scheduled()
    assert frozen.status == "frozen"
    assert frozen.frozen is not None
    assert [item.code for item in frozen.frozen.decision.entries] == ["600001"]
    assert [anchor.code for anchor in frozen.frozen.anchors] == ["600001"]


def test_persistence_failure_keeps_sealed_decision_and_retries_same_freeze() -> None:
    clock = _Clock(BOUNDARY)
    index = CurrentDecisionIndex()
    repository = _Repository()
    decision = _decision(1, BOUNDARY - timedelta(seconds=1))
    index.publish(decision, expected_current_version=None)
    coordinator = _coordinator(index, repository, clock)
    repository.fail_commit = True

    failed = coordinator.freeze_scheduled()
    late = index.publish(
        _decision(2, BOUNDARY + timedelta(seconds=1)),
        expected_current_version=decision.version,
    )
    assert index.frozen() is None
    repository.fail_commit = False
    retried = coordinator.freeze_scheduled()

    assert failed.status == "persistence_failed"
    assert index.latest() == decision
    assert late.reason == "freeze_sealed"
    assert retried.status == "frozen"
    assert retried.frozen is not None
    assert failed.freeze_version == retried.frozen.version


def test_restart_recovers_only_valid_tomorrow_checkpoint() -> None:
    repository = _Repository()
    checkpoint_decision = _decision(1, BOUNDARY - timedelta(seconds=10))
    repository.checkpoint = TomorrowFreezeCheckpoint(
        decision=checkpoint_decision,
        boundary_at=BOUNDARY,
    )
    index = CurrentDecisionIndex()
    coordinator = _coordinator(
        index,
        repository,
        _Clock(BOUNDARY + timedelta(minutes=3)),
    )

    result = coordinator.freeze_scheduled()

    assert result.status == "frozen"
    assert result.frozen is not None
    assert result.frozen.freeze_kind == "checkpoint_recovery"
    assert index.latest() == checkpoint_decision
    assert repository.checkpoint is None


def test_close_fallback_requires_missing_formal_record_and_explicit_close_anchors() -> None:
    clock = _Clock(CLOSE - timedelta(seconds=1))
    index = CurrentDecisionIndex()
    repository = _Repository()
    decision = _close_decision(1, CLOSE + timedelta(seconds=5))
    coordinator = _coordinator(index, repository, clock)
    anchors = build_decision_anchors(decision)

    early = coordinator.freeze_close_fallback(
        decision=decision,
        closing_anchors=anchors,
        recovery_path="close_rebuild",
    )
    clock.current = CLOSE + timedelta(seconds=10)
    frozen = coordinator.freeze_close_fallback(
        decision=decision,
        closing_anchors=anchors,
        recovery_path="close_rebuild",
    )
    duplicate = coordinator.freeze_close_fallback(
        decision=replace(decision, sequence=2),
        closing_anchors=anchors,
        recovery_path="close_rebuild",
    )

    assert early.status == "before_close_recovery"
    assert frozen.status == "frozen"
    assert frozen.frozen is not None
    assert frozen.frozen.freeze_kind == "close_fallback"
    assert "local_only" in frozen.frozen.degraded_reasons
    assert duplicate.status == "already_frozen"


def test_restart_rejects_checkpoint_from_a_different_runtime_identity() -> None:
    repository = _Repository()
    checkpoint_decision = replace(
        _decision(1, BOUNDARY - timedelta(seconds=10)),
        config_version="stale-config",
    )
    repository.checkpoint = TomorrowFreezeCheckpoint(
        decision=checkpoint_decision,
        boundary_at=BOUNDARY,
    )
    coordinator = _coordinator(
        CurrentDecisionIndex(),
        repository,
        _Clock(BOUNDARY + timedelta(minutes=3)),
    )

    result = coordinator.freeze_scheduled()

    assert result.status == "checkpoint_identity_mismatch"
    assert repository.frozen is None


def test_close_rebuild_rejects_decision_that_only_relabels_selected_anchors() -> None:
    decision = _decision(1, CLOSE)
    relabelled = tuple(
        replace(anchor, source="official_close", source_time=CLOSE) for anchor in build_decision_anchors(decision)
    )
    coordinator = _coordinator(
        CurrentDecisionIndex(),
        _Repository(),
        _Clock(CLOSE + timedelta(seconds=1)),
    )

    result = coordinator.freeze_close_fallback(
        decision=decision,
        closing_anchors=relabelled,
        recovery_path="close_rebuild",
    )

    assert result.status == "invalid_close_rebuild"


def test_checkpoint_read_failure_does_not_block_current_local_freeze() -> None:
    repository = _Repository()
    repository.fail_checkpoint_load = True
    index = CurrentDecisionIndex()
    decision = _decision(1, BOUNDARY - timedelta(seconds=1))
    index.publish(decision, expected_current_version=None)
    coordinator = _coordinator(index, repository, _Clock(BOUNDARY))

    result = coordinator.freeze_scheduled()

    assert result.status == "frozen"
    assert result.frozen is not None
    assert result.frozen.degraded_reasons == ("checkpoint_unavailable",)


def _decision(sequence: int, observed_at: datetime):
    evaluations = tuple(_evaluation(index, local_score=90.0 - index) for index in range(3))
    request = replace(
        _request(_selection(evaluations)),
        sequence=sequence,
        observed_at=observed_at,
    )
    return build_tomorrow_decision_epoch(request)


def _close_decision(sequence: int, observed_at: datetime):
    decision = _decision(sequence, observed_at)
    entries = tuple(
        replace(
            entry,
            features=replace(
                entry.features,
                observed_at=observed_at,
                quote=replace(
                    entry.features.quote,
                    source="official_close",
                    source_time=CLOSE,
                    received_time=observed_at,
                    data_version="official-close-v1",
                ),
            ),
        )
        for entry in decision.entries
    )
    return replace(decision, entries=entries)


def _coordinator(
    index: CurrentDecisionIndex,
    repository: _Repository,
    clock: _Clock,
) -> TomorrowFreezeCoordinator:
    return TomorrowFreezeCoordinator(
        index,
        repository,
        clock,
        runtime_identity=DecisionRuntimeIdentity(
            config_version="runtime-v2",
            strategy_version="tomorrow-v2",
            fusion_version="fusion_local68_deepseek32",
        ),
    )
