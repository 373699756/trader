"""Tomorrow formal checkpoint, freeze seal, retry, and close recovery on V2 identities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Literal
from zoneinfo import ZoneInfo

from trader.application.decision_core import UnifiedDecisionIndex
from trader.application.ports.clock import Clock
from trader.application.ports.decision_records import (
    DecisionRecordError,
    DecisionRecordRepositoryPort,
    V2DecisionCheckpoint,
)
from trader.domain.recommendation.decision_identity import (
    CommitKind,
    CommittedDecisionRecord,
    ScoredDecision,
    formal_scored_decision,
)
from trader.domain.recommendation.models import Strategy

SHANGHAI = ZoneInfo("Asia/Shanghai")
CloseRecoveryPath = Literal["current", "close_rebuild"]


@dataclass(frozen=True)
class V2FreezeOperationResult:
    status: str
    record: CommittedDecisionRecord | None = None
    version: str | None = None


@dataclass(frozen=True)
class V2DecisionRuntimeIdentity:
    config_version: str
    strategy_version: str
    fusion_version: str

    def __post_init__(self) -> None:
        if not all((self.config_version, self.strategy_version, self.fusion_version)):
            raise ValueError("V2 decision runtime identity must not be empty")

    def matches(self, decision: ScoredDecision) -> bool:
        return (
            decision.config_version == self.config_version
            and decision.strategy_version == self.strategy_version
            and decision.fusion_version == self.fusion_version
        )


class TomorrowV2FreezeCoordinator:
    """Owns the only formal Tomorrow V2 record transition for a trade date."""

    def __init__(
        self,
        index: UnifiedDecisionIndex,
        repository: DecisionRecordRepositoryPort,
        clock: Clock,
        *,
        runtime_identity: V2DecisionRuntimeIdentity,
    ) -> None:
        self._index = index
        self._repository = repository
        self._clock = clock
        self._runtime_identity = runtime_identity

    def capture_checkpoint(self) -> V2FreezeOperationResult:
        now = _now(self._clock)
        boundary = _at(now.date(), time(14, 50))
        if not _at(now.date(), time(14, 49, 20)) <= now < boundary:
            return V2FreezeOperationResult("outside_checkpoint_window")
        current = self._current(now.date())
        if current is None or current.observed_at > now:
            return V2FreezeOperationResult("no_eligible_decision")
        if not self._runtime_identity.matches(current):
            return V2FreezeOperationResult("runtime_identity_mismatch")
        if not 0.0 <= (boundary - current.observed_at).total_seconds() <= 30.0:
            return V2FreezeOperationResult("checkpoint_too_old")
        checkpoint = V2DecisionCheckpoint(formal_scored_decision(current), boundary)
        try:
            self._repository.save_checkpoint(checkpoint)
        except (DecisionRecordError, OSError):
            return V2FreezeOperationResult("persistence_failed", version=checkpoint.version)
        return V2FreezeOperationResult("checkpoint_saved", version=checkpoint.version)

    def freeze_scheduled(self) -> V2FreezeOperationResult:
        now = _now(self._clock)
        boundary = _at(now.date(), time(14, 50))
        if now < boundary:
            return V2FreezeOperationResult("before_freeze")
        existing = self._existing(now.date())
        if existing is not None:
            return existing
        checkpoint, checkpoint_failed = self._checkpoint(now.date(), boundary)
        current = self._current(now.date())
        if current is not None and not self._runtime_identity.matches(current):
            return V2FreezeOperationResult("runtime_identity_mismatch")
        seal = self._index.seal_for_freeze(
            Strategy.TOMORROW,
            boundary_at=boundary,
            fallback_decision=checkpoint.decision if checkpoint is not None else None,
        )
        if not seal.accepted or seal.decision is None:
            status = "checkpoint_unavailable" if checkpoint_failed else seal.reason
            return V2FreezeOperationResult(status)
        commit_kind: CommitKind = "checkpoint_recovery" if seal.source == "checkpoint" else "scheduled"
        record = CommittedDecisionRecord(seal.decision, boundary, commit_kind)
        committed = self._commit(record)
        if committed.status == "frozen" and checkpoint is not None:
            try:
                self._repository.consume_checkpoint(checkpoint, consumed_at=now)
            except (DecisionRecordError, OSError):
                pass
        return committed

    def freeze_close_fallback(
        self,
        decision: ScoredDecision,
        *,
        recovery_path: CloseRecoveryPath,
        official_close_version: str,
    ) -> V2FreezeOperationResult:
        now = _now(self._clock)
        close = _at(now.date(), time(15, 0))
        if now < close:
            return V2FreezeOperationResult("before_close_recovery")
        existing = self._existing(now.date())
        if existing is not None:
            return existing
        rejection = self._close_rejection(
            decision,
            recovery_path=recovery_path,
            official_close_version=official_close_version,
            now=now,
        )
        if rejection is not None:
            return V2FreezeOperationResult(rejection)
        seal = self._index.seal_close_fallback(
            decision,
            boundary_at=max(close, decision.observed_at),
            official_close_version=official_close_version,
        )
        if not seal.accepted or seal.decision is None:
            return V2FreezeOperationResult(seal.reason)
        record = CommittedDecisionRecord(seal.decision, max(close, decision.observed_at), "close_fallback")
        return self._commit(record)

    def restore(self, trade_date: date) -> V2FreezeOperationResult:
        return self._existing(trade_date) or V2FreezeOperationResult("formal_decision_unavailable")

    def _close_rejection(
        self,
        decision: ScoredDecision,
        *,
        recovery_path: CloseRecoveryPath,
        official_close_version: str,
        now: datetime,
    ) -> str | None:
        current = self._current(now.date())
        rejections = (
            (self._index.is_sealed(Strategy.TOMORROW, now.date()), "scheduled_freeze_pending"),
            (
                decision.strategy is not Strategy.TOMORROW or decision.trade_date != now.date(),
                "no_eligible_decision",
            ),
            (
                decision.observed_at > now or not official_close_version.startswith("official-close:"),
                "invalid_official_close",
            ),
            (not self._runtime_identity.matches(decision), "runtime_identity_mismatch"),
            (recovery_path == "current" and current != decision, "current_decision_mismatch"),
            (recovery_path == "close_rebuild" and decision.stage != "local", "close_rebuild_must_be_local"),
        )
        return next((reason for rejected, reason in rejections if rejected), None)

    def _current(self, trade_date: date) -> ScoredDecision | None:
        current = self._index.snapshot(Strategy.TOMORROW).current
        return current if isinstance(current, ScoredDecision) and current.trade_date == trade_date else None

    def _existing(self, trade_date: date) -> V2FreezeOperationResult | None:
        try:
            record = self._repository.load(Strategy.TOMORROW, trade_date)
        except (DecisionRecordError, OSError):
            return V2FreezeOperationResult("persistence_failed")
        if record is None:
            return None
        if not self._index.restore_formal(record):
            return V2FreezeOperationResult("index_restore_conflict", record, record.version)
        return V2FreezeOperationResult("already_frozen", record, record.version)

    def _checkpoint(
        self,
        trade_date: date,
        boundary: datetime,
    ) -> tuple[V2DecisionCheckpoint | None, bool]:
        try:
            checkpoint = self._repository.load_checkpoint(Strategy.TOMORROW, trade_date)
        except (DecisionRecordError, OSError):
            return None, True
        if checkpoint is None:
            return None, False
        if (
            checkpoint.boundary_at != boundary
            or not self._runtime_identity.matches(checkpoint.decision)
            or not 0.0 <= (boundary - checkpoint.decision.observed_at).total_seconds() <= 30.0
        ):
            return None, False
        return checkpoint, False

    def _commit(self, record: CommittedDecisionRecord) -> V2FreezeOperationResult:
        try:
            self._repository.commit(record)
        except (DecisionRecordError, OSError):
            return V2FreezeOperationResult("persistence_failed", version=record.version)
        if not self._index.commit_formal(record):
            return V2FreezeOperationResult("index_commit_conflict", version=record.version)
        return V2FreezeOperationResult("frozen", record, record.version)


def _now(clock: Clock) -> datetime:
    value = clock.now()
    if value.tzinfo is None or value.utcoffset() is None or getattr(value.tzinfo, "key", None) != SHANGHAI.key:
        raise ValueError("clock must return Asia/Shanghai time")
    return value


def _at(trade_date: date, value: time) -> datetime:
    return datetime.combine(trade_date, value, tzinfo=SHANGHAI)


__all__ = [
    "CloseRecoveryPath",
    "TomorrowV2FreezeCoordinator",
    "V2DecisionRuntimeIdentity",
    "V2FreezeOperationResult",
]
