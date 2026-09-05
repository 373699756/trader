"""Today exact-boundary freeze with permanent missed-freeze closure."""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from trader.application.ports.clock import Clock
from trader.application.ports.decision_index import DecisionIndexPort
from trader.application.ports.decision_records import DecisionRecordError, DecisionRecordRepositoryPort
from trader.application.recommendation.scored_freezing import DecisionRuntimeIdentity, FreezeOperationResult
from trader.domain.recommendation.decision_identity import CommittedDecisionRecord, ScoredDecision
from trader.domain.recommendation.models import Strategy

SHANGHAI = ZoneInfo("Asia/Shanghai")


class TodayFreezeCoordinator:
    """Owns Today's no-recovery 11:20 formal transition."""

    def __init__(
        self,
        index: DecisionIndexPort,
        repository: DecisionRecordRepositoryPort,
        clock: Clock,
        *,
        runtime_identity: DecisionRuntimeIdentity,
    ) -> None:
        self._index = index
        self._repository = repository
        self._clock = clock
        self._runtime_identity = runtime_identity

    def initialize(self) -> FreezeOperationResult:
        now = _now(self._clock)
        if now >= _at(now.date(), time(11, 20)):
            self._close(now.date())
        existing = self._existing(now.date())
        if existing is not None:
            if now >= _at(now.date(), time(11, 20)) and existing.status == "persistence_failed":
                self._index.discard_closed_current(Strategy.TODAY, now.date())
            return existing
        if self._index.is_closed(Strategy.TODAY, now.date()):
            self._index.discard_closed_current(Strategy.TODAY, now.date())
            return FreezeOperationResult("missed_freeze")
        return FreezeOperationResult("ready_for_freeze")

    def freeze_scheduled(self) -> FreezeOperationResult:
        now = _now(self._clock)
        boundary = _at(now.date(), time(11, 20))
        if now < boundary:
            return FreezeOperationResult("before_freeze")

        return self._freeze_at_or_after_boundary(now, boundary)

    def _freeze_at_or_after_boundary(
        self,
        now: datetime,
        boundary: datetime,
    ) -> FreezeOperationResult:
        already_closed = self._index.is_closed(Strategy.TODAY, now.date())
        already_sealed = self._index.is_sealed(Strategy.TODAY, now.date())
        self._close(now.date())
        existing = self._existing(now.date())
        repository_unavailable = existing is not None and existing.status == "persistence_failed"
        if existing is not None and not repository_unavailable:
            return existing

        missed = self._missed_result(
            now=now,
            boundary=boundary,
            already_closed=already_closed,
            already_sealed=already_sealed,
            repository_unavailable=repository_unavailable,
        )
        if missed is not None:
            return missed
        current = self._current(now.date())
        if current is not None and not self._runtime_identity.matches(current):
            self._index.discard_closed_current(Strategy.TODAY, now.date())
            return FreezeOperationResult("runtime_identity_mismatch")
        return self._seal_and_commit(boundary, repository_unavailable=repository_unavailable)

    def _missed_result(
        self,
        *,
        now: datetime,
        boundary: datetime,
        already_closed: bool,
        already_sealed: bool,
        repository_unavailable: bool,
    ) -> FreezeOperationResult | None:
        if already_sealed:
            return None
        if now.replace(microsecond=0) == boundary and not already_closed:
            return None
        self._index.discard_closed_current(Strategy.TODAY, now.date())
        status = "persistence_failed" if repository_unavailable and now > boundary else "missed_freeze"
        return FreezeOperationResult(status)

    def _seal_and_commit(
        self,
        boundary: datetime,
        *,
        repository_unavailable: bool,
    ) -> FreezeOperationResult:
        seal = self._index.seal_for_freeze(Strategy.TODAY, boundary_at=boundary)
        if not seal.accepted or seal.decision is None:
            self._index.discard_closed_current(Strategy.TODAY, boundary.date())
            return FreezeOperationResult("missed_freeze")
        record = CommittedDecisionRecord(seal.decision, boundary, "scheduled")
        if repository_unavailable:
            return FreezeOperationResult("persistence_failed", version=record.version)
        try:
            self._repository.commit(record)
        except (DecisionRecordError, OSError):
            return FreezeOperationResult("persistence_failed", version=record.version)
        if not self._index.commit_formal(record):
            return FreezeOperationResult("index_commit_conflict", version=record.version)
        return FreezeOperationResult("frozen", record, record.version)

    def _close(self, trade_date: date) -> None:
        boundary = _at(trade_date, time(11, 20))
        if not self._index.close_for_date(Strategy.TODAY, trade_date, boundary_at=boundary):
            raise RuntimeError("Today freeze close conflicts with another trade date")

    def _current(self, trade_date: date) -> ScoredDecision | None:
        current = self._index.snapshot(Strategy.TODAY).current
        return current if isinstance(current, ScoredDecision) and current.trade_date == trade_date else None

    def _existing(self, trade_date: date) -> FreezeOperationResult | None:
        try:
            record = self._repository.load(Strategy.TODAY, trade_date)
        except (DecisionRecordError, OSError):
            return FreezeOperationResult("persistence_failed")
        if record is None:
            return None
        if not self._index.restore_formal(record):
            return FreezeOperationResult("index_restore_conflict", record, record.version)
        return FreezeOperationResult("already_frozen", record, record.version)


def _now(clock: Clock) -> datetime:
    value = clock.now()
    if value.tzinfo is None or value.utcoffset() is None or getattr(value.tzinfo, "key", None) != SHANGHAI.key:
        raise ValueError("clock must return Asia/Shanghai time")
    return value


def _at(trade_date: date, value: time) -> datetime:
    return datetime.combine(trade_date, value, tzinfo=SHANGHAI)


__all__ = ["TodayFreezeCoordinator"]
