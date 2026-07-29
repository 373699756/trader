"""Application coordination for tomorrow checkpoint and immutable freeze."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Literal
from zoneinfo import ZoneInfo

from trader.application.current_decisions import CurrentDecisionIndex
from trader.application.official_records import official_decision
from trader.application.ports.clock import Clock
from trader.application.ports.decision_freezes import (
    DecisionFreezeError,
    TomorrowDecisionFreezePort,
)
from trader.domain.recommendation.tomorrow_freeze import (
    DecisionAnchor,
    FreezeKind,
    TomorrowDecisionFreeze,
    TomorrowFreezeCheckpoint,
    build_decision_anchors,
)
from trader.domain.recommendation.tomorrow_fusion import DecisionEpoch

SHANGHAI = ZoneInfo("Asia/Shanghai")
RecoveryPath = Literal["p6", "close_rebuild"]


@dataclass(frozen=True)
class FreezeOperationResult:
    status: str
    frozen: TomorrowDecisionFreeze | None = None
    freeze_version: str | None = None


@dataclass(frozen=True)
class DecisionRuntimeIdentity:
    config_version: str
    strategy_version: str
    fusion_version: str

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (
                self.config_version,
                self.strategy_version,
                self.fusion_version,
            )
        ):
            raise ValueError("decision runtime identity must not be empty")

    def matches(self, decision: DecisionEpoch) -> bool:
        return (
            decision.config_version == self.config_version
            and decision.strategy_version == self.strategy_version
            and decision.fusion_version == self.fusion_version
        )


class TomorrowFreezeCoordinator:
    def __init__(
        self,
        index: CurrentDecisionIndex,
        repository: TomorrowDecisionFreezePort,
        clock: Clock,
        *,
        runtime_identity: DecisionRuntimeIdentity,
    ) -> None:
        self._index = index
        self._repository = repository
        self._clock = clock
        self._runtime_identity = runtime_identity

    def capture_checkpoint(self) -> FreezeOperationResult:
        now = _shanghai_now(self._clock)
        boundary = _at(now.date(), time(14, 50))
        if not _at(now.date(), time(14, 49, 20)) <= now < boundary:
            return FreezeOperationResult("outside_checkpoint_window")
        decision = self._index.latest()
        if decision is None or decision.trade_date != now.date() or decision.observed_at > now:
            return FreezeOperationResult("no_eligible_decision")
        if not self._runtime_identity.matches(decision):
            return FreezeOperationResult("runtime_identity_mismatch")
        try:
            checkpoint = TomorrowFreezeCheckpoint(decision=official_decision(decision), boundary_at=boundary)
        except ValueError:
            return FreezeOperationResult("no_eligible_decision")
        try:
            self._repository.save_checkpoint(checkpoint)
        except (DecisionFreezeError, OSError):
            return FreezeOperationResult("persistence_failed")
        return FreezeOperationResult("checkpoint_saved", freeze_version=checkpoint.version)

    def freeze_scheduled(self) -> FreezeOperationResult:
        now = _shanghai_now(self._clock)
        boundary = _at(now.date(), time(14, 50))
        if now < boundary:
            return FreezeOperationResult("before_freeze")
        existing = self._load_existing(now.date())
        if existing is not None:
            return existing
        checkpoint, checkpoint_unavailable = self._load_checkpoint(now.date())
        checkpoint_identity_mismatch = False
        if checkpoint is not None and not self._runtime_identity.matches(checkpoint.decision):
            checkpoint = None
            checkpoint_identity_mismatch = True
        fallback = checkpoint.decision if checkpoint is not None else None
        current = self._index.latest()
        if (
            current is not None
            and current.trade_date == now.date()
            and current.observed_at <= boundary
            and not self._runtime_identity.matches(current)
        ):
            return FreezeOperationResult("runtime_identity_mismatch")
        seal = self._index.seal_for_freeze(
            boundary_at=boundary,
            fallback_decision=fallback,
        )
        if not seal.accepted or seal.decision is None:
            return _seal_rejection(
                seal.reason,
                checkpoint_unavailable=checkpoint_unavailable,
                checkpoint_identity_mismatch=checkpoint_identity_mismatch,
            )
        freeze_kind: FreezeKind = "checkpoint_recovery" if seal.source == "fallback" else "scheduled"
        official = official_decision(seal.decision)
        frozen = TomorrowDecisionFreeze(
            decision=official,
            frozen_at=boundary,
            freeze_kind=freeze_kind,
            anchors=build_decision_anchors(official),
            checkpoint_version=checkpoint.version if freeze_kind == "checkpoint_recovery" and checkpoint else None,
            degraded_reasons=("checkpoint_unavailable",) if checkpoint_unavailable else (),
        )
        committed = self._commit(frozen)
        if committed.status == "frozen" and checkpoint is not None:
            self._consume_checkpoint(checkpoint, now)
        return committed

    def freeze_close_fallback(
        self,
        *,
        decision: DecisionEpoch,
        closing_anchors: tuple[DecisionAnchor, ...],
        recovery_path: RecoveryPath,
    ) -> FreezeOperationResult:
        now = _shanghai_now(self._clock)
        close = _at(now.date(), time(15, 0))
        if now < close:
            return FreezeOperationResult("before_close_recovery")
        existing = self._load_existing(now.date())
        if existing is not None:
            return existing
        rejection = self._close_rejection(
            decision=decision,
            closing_anchors=closing_anchors,
            recovery_path=recovery_path,
            now=now,
            close=close,
        )
        if rejection is not None:
            return FreezeOperationResult(rejection)
        frozen_at = max(close, decision.observed_at)
        seal = self._index.seal_close_fallback(decision, boundary_at=frozen_at)
        if not seal.accepted:
            return FreezeOperationResult(seal.reason)
        reasons = ["close_fallback", "official_close"]
        if decision.projection_stage == "local":
            reasons.append("local_only")
        official = official_decision(decision)
        official_codes = {item.code for item in official.entries}
        frozen = TomorrowDecisionFreeze(
            decision=official,
            frozen_at=frozen_at,
            freeze_kind="close_fallback",
            anchors=tuple(anchor for anchor in closing_anchors if anchor.code in official_codes),
            degraded_reasons=tuple(reasons),
        )
        return self._commit(frozen)

    def _close_rejection(
        self,
        *,
        decision: DecisionEpoch,
        closing_anchors: tuple[DecisionAnchor, ...],
        recovery_path: RecoveryPath,
        now: datetime,
        close: datetime,
    ) -> str | None:
        reason: str | None = None
        if self._index.is_sealed(now.date()):
            reason = "scheduled_freeze_pending"
        elif decision.trade_date != now.date() or decision.observed_at > now:
            reason = "no_eligible_decision"
        elif not self._runtime_identity.matches(decision):
            reason = "runtime_identity_mismatch"
        elif recovery_path == "p6" and self._index.latest() != decision:
            reason = "p6_decision_mismatch"
        elif recovery_path == "close_rebuild" and decision.projection_stage != "local":
            reason = "close_rebuild_must_be_local"
        elif recovery_path == "close_rebuild" and not _is_official_close_rebuild(
            decision,
            closing_anchors,
            close,
        ):
            reason = "invalid_close_rebuild"
        return reason

    def _load_existing(self, trade_date: date) -> FreezeOperationResult | None:
        try:
            existing = self._repository.load_frozen(trade_date)
        except (DecisionFreezeError, OSError):
            return FreezeOperationResult("persistence_failed")
        if existing is None:
            return None
        self._index.restore_frozen(existing)
        return FreezeOperationResult("already_frozen", existing, existing.version)

    def _load_checkpoint(
        self,
        trade_date: date,
    ) -> tuple[TomorrowFreezeCheckpoint | None, bool]:
        try:
            return self._repository.load_checkpoint(trade_date), False
        except (DecisionFreezeError, OSError):
            return None, True

    def _commit(self, frozen: TomorrowDecisionFreeze) -> FreezeOperationResult:
        try:
            self._repository.commit_freeze(frozen)
        except (DecisionFreezeError, OSError):
            return FreezeOperationResult("persistence_failed", freeze_version=frozen.version)
        if not self._index.commit_frozen(frozen):
            return FreezeOperationResult("index_commit_conflict", freeze_version=frozen.version)
        return FreezeOperationResult("frozen", frozen, frozen.version)

    def _consume_checkpoint(
        self,
        checkpoint: TomorrowFreezeCheckpoint,
        consumed_at: datetime,
    ) -> None:
        try:
            self._repository.consume_checkpoint(
                checkpoint.version,
                consumed_at=consumed_at,
            )
        except (DecisionFreezeError, OSError):
            pass


def _shanghai_now(clock: Clock) -> datetime:
    value = clock.now()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")
    if getattr(value.tzinfo, "key", None) != "Asia/Shanghai":
        raise ValueError("clock must return Asia/Shanghai time")
    return value


def _at(trade_date: date, local_time: time) -> datetime:
    return datetime.combine(trade_date, local_time, tzinfo=SHANGHAI)


def _seal_rejection(
    reason: str,
    *,
    checkpoint_unavailable: bool,
    checkpoint_identity_mismatch: bool,
) -> FreezeOperationResult:
    if checkpoint_unavailable:
        reason = "checkpoint_unavailable"
    elif checkpoint_identity_mismatch:
        reason = "checkpoint_identity_mismatch"
    return FreezeOperationResult(reason)


def _is_official_close_rebuild(
    decision: DecisionEpoch,
    anchors: tuple[DecisionAnchor, ...],
    close: datetime,
) -> bool:
    if build_decision_anchors(decision) != anchors:
        return False
    return all(
        entry.features.quote.source == "official_close"
        and entry.features.quote.source_time >= close
        and entry.features.observed_at >= close
        for entry in decision.entries
    )


__all__ = [
    "DecisionRuntimeIdentity",
    "FreezeOperationResult",
    "RecoveryPath",
    "TomorrowFreezeCoordinator",
]
