"""Formal checkpoint, first-wins freeze, and close recovery coordination."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Literal
from zoneinfo import ZoneInfo

from trader.recommendation.application.pipeline.freeze_publish.publication_io import (
    PublicationIoTarget,
    PublicationIoTracker,
    observe_publication_io,
)
from trader.recommendation.application.ports.clock import Clock
from trader.recommendation.application.ports.decision_index import DecisionIndexPort
from trader.recommendation.application.ports.decision_records import (
    DecisionCheckpoint,
    DecisionRecordError,
    DecisionRecordPort,
)
from trader.recommendation.domain.evidence.pipeline import StageState
from trader.recommendation.domain.publication.decision_identity import (
    CommitKind,
    CommittedDecisionRecord,
    ScoredDecision,
    formal_scored_decision,
)
from trader.recommendation.domain.publication.models import Strategy

SHANGHAI = ZoneInfo("Asia/Shanghai")
CloseRecoveryPath = Literal["current", "close_rebuild"]


@dataclass(frozen=True)
class FreezeOperationResult:
    status: str
    record: CommittedDecisionRecord | None = None
    version: str | None = None


@dataclass(frozen=True)
class DecisionRuntimeIdentity:
    config_version: str
    strategy_version: str
    fusion_version: str

    def __post_init__(self) -> None:
        if not all((self.config_version, self.strategy_version, self.fusion_version)):
            raise ValueError("decision runtime identity must not be empty")

    def matches(self, decision: ScoredDecision) -> bool:
        return (
            decision.config_version == self.config_version
            and decision.strategy_version == self.strategy_version
            and decision.fusion_version == self.fusion_version
        )


class ScoredFreezeCoordinator:
    """Owns the only formal scored record transition for a trade date."""

    def __init__(
        self,
        index: DecisionIndexPort,
        records: DecisionRecordPort,
        clock: Clock,
        *,
        runtime_identity: DecisionRuntimeIdentity,
        strategy: Strategy = Strategy.TOMORROW,
        publication_io: PublicationIoTracker | None = None,
    ) -> None:
        self._index = index
        self._records = records
        self._clock = clock
        self._runtime_identity = runtime_identity
        self._strategy = strategy
        self._publication_io = publication_io

    def capture_checkpoint(self) -> FreezeOperationResult:
        now = _now(self._clock)
        boundary = _at(now.date(), time(15, 0))
        if not _at(now.date(), time(14, 59, 20)) <= now < boundary:
            return FreezeOperationResult("outside_checkpoint_window")
        current = self._current(now.date())
        if current is None or current.observed_at > now:
            return FreezeOperationResult("no_eligible_decision")
        if not self._runtime_identity.matches(current):
            return FreezeOperationResult("runtime_identity_mismatch")
        if not 0.0 <= (boundary - current.observed_at).total_seconds() <= 30.0:
            return FreezeOperationResult("checkpoint_too_old")
        checkpoint = DecisionCheckpoint(formal_scored_decision(current), boundary)
        try:
            with observe_publication_io(
                self._publication_io, "checkpoint_write", PublicationIoTarget.decision(current)
            ) as observation:
                self._records.save_checkpoint(checkpoint)
                observation.output_version = checkpoint.version
        except (DecisionRecordError, OSError):
            return FreezeOperationResult("persistence_failed", version=checkpoint.version)
        return FreezeOperationResult("checkpoint_saved", version=checkpoint.version)

    def freeze_scheduled(self) -> FreezeOperationResult:
        now = _now(self._clock)
        boundary = _at(now.date(), time(15, 0))
        if now < boundary:
            return FreezeOperationResult("before_freeze")
        existing = self._existing(now.date())
        if existing is not None:
            return existing
        checkpoint, checkpoint_failed = self._checkpoint(now.date(), boundary)
        current = self._current(now.date())
        if current is not None and not self._runtime_identity.matches(current):
            return FreezeOperationResult("runtime_identity_mismatch")
        with observe_publication_io(
            self._publication_io,
            "freeze_seal",
            self._target(now.date(), current or (checkpoint.decision if checkpoint is not None else None)),
        ) as observation:
            seal = self._index.seal_for_freeze(
                self._strategy,
                boundary_at=boundary,
                fallback_decision=checkpoint.decision if checkpoint is not None else None,
            )
            observation.reason = seal.reason
            observation.state = StageState.READY if seal.accepted else StageState.NOT_READY
            observation.output_version = seal.decision.version if seal.decision is not None else None
        if not seal.accepted or seal.decision is None:
            status = "checkpoint_unavailable" if checkpoint_failed else seal.reason
            return FreezeOperationResult(status)
        commit_kind: CommitKind = "checkpoint_recovery" if seal.source == "checkpoint" else "scheduled"
        record = CommittedDecisionRecord(seal.decision, boundary, commit_kind)
        committed = self._commit(record)
        if committed.status == "frozen" and checkpoint is not None:
            try:
                with observe_publication_io(
                    self._publication_io, "checkpoint_consume", PublicationIoTarget.decision(seal.decision)
                ) as observation:
                    self._records.consume_checkpoint(checkpoint, consumed_at=now)
                    observation.output_version = checkpoint.version
            except (DecisionRecordError, OSError):
                pass
        return committed

    def freeze_close_fallback(
        self,
        decision: ScoredDecision,
        *,
        recovery_path: CloseRecoveryPath,
        official_close_version: str,
    ) -> FreezeOperationResult:
        now = _now(self._clock)
        close = _at(now.date(), time(15, 0))
        if now < close:
            return FreezeOperationResult("before_close_recovery")
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
            return FreezeOperationResult(rejection)
        with observe_publication_io(
            self._publication_io, "freeze_seal", PublicationIoTarget.decision(decision)
        ) as observation:
            seal = self._index.seal_close_fallback(
                decision,
                boundary_at=max(close, decision.observed_at),
                official_close_version=official_close_version,
            )
            observation.reason = seal.reason
            observation.state = StageState.READY if seal.accepted else StageState.NOT_READY
            observation.output_version = seal.decision.version if seal.decision is not None else None
        if not seal.accepted or seal.decision is None:
            return FreezeOperationResult(seal.reason)
        record = CommittedDecisionRecord(seal.decision, max(close, decision.observed_at), "close_fallback")
        return self._commit(record)

    def restore(self, trade_date: date) -> FreezeOperationResult:
        return self._existing(trade_date) or FreezeOperationResult("formal_decision_unavailable")

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
            (
                decision.strategy is not self._strategy or decision.trade_date != now.date(),
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
        current = self._index.snapshot(self._strategy).current
        return current if isinstance(current, ScoredDecision) and current.trade_date == trade_date else None

    def _existing(self, trade_date: date) -> FreezeOperationResult | None:
        try:
            with observe_publication_io(self._publication_io, "formal_lookup", self._target(trade_date)) as observation:
                record = self._records.load(self._strategy, trade_date)
                observation.reason = "formal_found" if record is not None else "formal_missing"
                observation.state = StageState.READY if record is not None else StageState.NOT_READY
                if record is not None:
                    observation.target = PublicationIoTarget.decision(record.decision)
                    observation.output_version = record.version
        except (DecisionRecordError, OSError):
            return FreezeOperationResult("persistence_failed")
        if record is None:
            return None
        with observe_publication_io(
            self._publication_io, "formal_restore", PublicationIoTarget.decision(record.decision)
        ) as observation:
            restored = self._index.restore_formal(record)
            observation.reason = "already_frozen" if restored else "index_restore_conflict"
            observation.state = StageState.READY if restored else StageState.FAILED
            observation.output_version = record.decision.version if restored else None
        if not restored:
            return FreezeOperationResult("index_restore_conflict", record, record.version)
        return FreezeOperationResult("already_frozen", record, record.version)

    def _checkpoint(
        self,
        trade_date: date,
        boundary: datetime,
    ) -> tuple[DecisionCheckpoint | None, bool]:
        try:
            with observe_publication_io(
                self._publication_io, "checkpoint_lookup", self._target(trade_date)
            ) as observation:
                checkpoint = self._records.load_checkpoint(self._strategy, trade_date)
                observation.reason = "checkpoint_found" if checkpoint is not None else "checkpoint_missing"
                observation.state = StageState.READY if checkpoint is not None else StageState.NOT_READY
                if checkpoint is not None:
                    observation.target = PublicationIoTarget.decision(checkpoint.decision)
                    observation.output_version = checkpoint.version
                    if (
                        checkpoint.boundary_at != boundary
                        or not self._runtime_identity.matches(checkpoint.decision)
                        or not 0.0 <= (boundary - checkpoint.decision.observed_at).total_seconds() <= 30.0
                    ):
                        observation.reason = "checkpoint_ineligible"
                        observation.state = StageState.NOT_READY
                        observation.output_version = None
                        return None, False
        except (DecisionRecordError, OSError):
            return None, True
        if checkpoint is None:
            return None, False
        return checkpoint, False

    def _commit(self, record: CommittedDecisionRecord) -> FreezeOperationResult:
        try:
            with observe_publication_io(
                self._publication_io, "formal_write", PublicationIoTarget.decision(record.decision)
            ) as observation:
                self._records.commit(record)
                observation.output_version = record.version
        except (DecisionRecordError, OSError):
            return FreezeOperationResult("persistence_failed", version=record.version)
        with observe_publication_io(
            self._publication_io, "formal_publish", PublicationIoTarget.decision(record.decision)
        ) as observation:
            published = self._index.commit_formal(record)
            observation.reason = "frozen" if published else "index_commit_conflict"
            observation.state = StageState.READY if published else StageState.FAILED
            observation.output_version = record.decision.version if published else None
        if not published:
            return FreezeOperationResult("index_commit_conflict", version=record.version)
        return FreezeOperationResult("frozen", record, record.version)

    def _target(self, trade_date: date, decision: ScoredDecision | None = None) -> PublicationIoTarget:
        value = decision or self._current(trade_date)
        return (
            PublicationIoTarget.decision(value)
            if value is not None
            else PublicationIoTarget(self._strategy, trade_date, None, 0)
        )


def _now(clock: Clock) -> datetime:
    value = clock.now()
    if (
        value.tzinfo is None
        or value.utcoffset() is None
        or not (isinstance(value.tzinfo, ZoneInfo) and value.tzinfo.key == SHANGHAI.key)
    ):
        raise ValueError("clock must return Asia/Shanghai time")
    return value


def _at(trade_date: date, value: time) -> datetime:
    return datetime.combine(trade_date, value, tzinfo=SHANGHAI)


__all__ = [
    "CloseRecoveryPath",
    "ScoredFreezeCoordinator",
    "DecisionRuntimeIdentity",
    "FreezeOperationResult",
]
