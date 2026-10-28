"""First-wins freeze and close recovery coordination."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Literal
from zoneinfo import ZoneInfo

from trader.recommendation.application.pipeline.freeze_publish.publication_io import (
    ObservePublicationIo,
    PublicationIoTarget,
    PublicationIoTracker,
)
from trader.recommendation.application.ports.clock import Clock
from trader.recommendation.application.ports.freeze_index import FreezeIndexPort
from trader.recommendation.application.ports.freeze_records import (
    FreezeRecordError,
    FreezeRecordPort,
)
from trader.recommendation.domain.evidence.pipeline import StageState
from trader.recommendation.domain.publication.freeze_identity import (
    CommittedFreezeRecord,
    IsPreviewFreeze,
    IsShadowFreeze,
    ScoredFreeze,
)
from trader.recommendation.domain.publication.models import Strategy

SHANGHAI = ZoneInfo("Asia/Shanghai")
CloseRecoveryPath = Literal["current", "close_rebuild"]


@dataclass(frozen=True)
class FreezeOperationResult:
    status: str
    record: CommittedFreezeRecord | None = None
    version: str | None = None


@dataclass(frozen=True)
class FreezeRuntimeIdentity:
    config_version: str
    strategy_version: str
    fusion_version: str

    def __post_init__(self) -> None:
        if not all((self.config_version, self.strategy_version, self.fusion_version)):
            raise ValueError("freeze runtime identity must not be empty")

    def matches(self, freeze: ScoredFreeze) -> bool:
        return (
            freeze.config_version == self.config_version
            and freeze.strategy_version == self.strategy_version
            and freeze.fusion_version == self.fusion_version
        )


class ScoredFreezeCoordinator:
    """Owns the only formal scored record transition for a trade date."""

    def __init__(
        self,
        index: FreezeIndexPort,
        records: FreezeRecordPort,
        clock: Clock,
        *,
        runtime_identity: FreezeRuntimeIdentity,
        strategy: Strategy = Strategy.TOMORROW,
        publication_io: PublicationIoTracker | None = None,
        production_authority: bool = True,
    ) -> None:
        self._index = index
        self._records = records
        self._clock = clock
        self._runtime_identity = runtime_identity
        self._strategy = strategy
        self._publication_io = publication_io
        self._production_authority = production_authority

    def freeze_scheduled(self) -> FreezeOperationResult:
        now = _now(self._clock)
        current = self._current(now.date())
        if current is not None and IsPreviewFreeze(current):
            return FreezeOperationResult("preview_only")
        boundary = _at(now.date(), time(15, 0))
        if now < boundary:
            return FreezeOperationResult("before_freeze")
        existing = self._existing(now.date())
        if existing is not None:
            return existing
        if not self._production_authority:
            return FreezeOperationResult("shadow_only")
        current = self._current(now.date())
        if current is not None and IsShadowFreeze(current):
            return FreezeOperationResult("shadow_only")
        if current is not None and not self._runtime_identity.matches(current):
            return FreezeOperationResult("runtime_identity_mismatch")
        with ObservePublicationIo(
            self._publication_io,
            "freeze_seal",
            self._target(now.date(), current),
        ) as observation:
            seal = self._index.seal_for_freeze(
                self._strategy,
                boundary_at=boundary,
            )
            observation.reason = seal.reason
            observation.state = StageState.READY if seal.accepted else StageState.NOT_READY
            observation.output_version = seal.freeze.version if seal.freeze is not None else None
        if not seal.accepted or seal.freeze is None:
            return FreezeOperationResult(seal.reason)
        record = CommittedFreezeRecord(seal.freeze, boundary, "scheduled")
        return self._commit(record)

    def freeze_close_fallback(
        self,
        freeze: ScoredFreeze,
        *,
        recovery_path: CloseRecoveryPath,
        official_close_version: str,
    ) -> FreezeOperationResult:
        if IsPreviewFreeze(freeze):
            return FreezeOperationResult("preview_only")
        now = _now(self._clock)
        close = _at(now.date(), time(15, 0))
        if now < close:
            return FreezeOperationResult("before_close_recovery")
        existing = self._existing(now.date())
        if existing is not None:
            return existing
        if not self._production_authority:
            return FreezeOperationResult("shadow_only")
        rejection = self._close_rejection(
            freeze,
            recovery_path=recovery_path,
            official_close_version=official_close_version,
            now=now,
        )
        if rejection is not None:
            return FreezeOperationResult(rejection)
        with ObservePublicationIo(
            self._publication_io, "freeze_seal", PublicationIoTarget.freeze(freeze)
        ) as observation:
            seal = self._index.seal_close_fallback(
                freeze,
                boundary_at=max(close, freeze.observed_at),
                official_close_version=official_close_version,
            )
            observation.reason = seal.reason
            observation.state = StageState.READY if seal.accepted else StageState.NOT_READY
            observation.output_version = seal.freeze.version if seal.freeze is not None else None
        if not seal.accepted or seal.freeze is None:
            return FreezeOperationResult(seal.reason)
        record = CommittedFreezeRecord(seal.freeze, max(close, freeze.observed_at), "close_fallback")
        return self._commit(record)

    def restore(self, trade_date: date) -> FreezeOperationResult:
        current = self._current(trade_date)
        if current is not None and IsPreviewFreeze(current):
            return FreezeOperationResult("preview_only")
        return self._existing(trade_date) or FreezeOperationResult("formal_freeze_unavailable")

    def _close_rejection(
        self,
        freeze: ScoredFreeze,
        *,
        recovery_path: CloseRecoveryPath,
        official_close_version: str,
        now: datetime,
    ) -> str | None:
        if IsShadowFreeze(freeze):
            return "shadow_only"
        current = self._current(now.date())
        rejections = (
            (
                freeze.strategy is not self._strategy or freeze.trade_date != now.date(),
                "no_eligible_freeze",
            ),
            (
                freeze.observed_at > now or not official_close_version.startswith("official-close:"),
                "invalid_official_close",
            ),
            (not self._runtime_identity.matches(freeze), "runtime_identity_mismatch"),
            (recovery_path == "current" and current != freeze, "current_freeze_mismatch"),
            (recovery_path == "close_rebuild" and freeze.stage != "local", "close_rebuild_must_be_local"),
        )
        return next((reason for rejected, reason in rejections if rejected), None)

    def _current(self, trade_date: date) -> ScoredFreeze | None:
        current = self._index.snapshot(self._strategy).current
        return current if isinstance(current, ScoredFreeze) and current.trade_date == trade_date else None

    def _existing(self, trade_date: date) -> FreezeOperationResult | None:
        try:
            with ObservePublicationIo(self._publication_io, "formal_lookup", self._target(trade_date)) as observation:
                record = self._records.load(self._strategy, trade_date)
                observation.reason = "formal_found" if record is not None else "formal_missing"
                observation.state = StageState.READY if record is not None else StageState.NOT_READY
                if record is not None:
                    observation.target = PublicationIoTarget.freeze(record.freeze)
                    observation.output_version = record.version
        except (FreezeRecordError, OSError):
            return FreezeOperationResult("persistence_failed")
        if record is None:
            return None
        with ObservePublicationIo(
            self._publication_io, "formal_restore", PublicationIoTarget.freeze(record.freeze)
        ) as observation:
            restored = self._index.restore_formal(record)
            observation.reason = "already_frozen" if restored else "index_restore_conflict"
            observation.state = StageState.READY if restored else StageState.FAILED
            observation.output_version = record.freeze.version if restored else None
        if not restored:
            return FreezeOperationResult("index_restore_conflict", record, record.version)
        return FreezeOperationResult("already_frozen", record, record.version)

    def _commit(self, record: CommittedFreezeRecord) -> FreezeOperationResult:
        if IsPreviewFreeze(record.freeze):
            return FreezeOperationResult("preview_only")
        try:
            with ObservePublicationIo(
                self._publication_io, "formal_write", PublicationIoTarget.freeze(record.freeze)
            ) as observation:
                self._records.commit(record)
                observation.output_version = record.version
        except (FreezeRecordError, OSError):
            return FreezeOperationResult("persistence_failed", version=record.version)
        with ObservePublicationIo(
            self._publication_io, "formal_publish", PublicationIoTarget.freeze(record.freeze)
        ) as observation:
            published = self._index.commit_formal(record)
            observation.reason = "frozen" if published else "index_commit_conflict"
            observation.state = StageState.READY if published else StageState.FAILED
            observation.output_version = record.freeze.version if published else None
        if not published:
            return FreezeOperationResult("index_commit_conflict", version=record.version)
        return FreezeOperationResult("frozen", record, record.version)

    def _target(self, trade_date: date, freeze: ScoredFreeze | None = None) -> PublicationIoTarget:
        value = freeze or self._current(trade_date)
        return (
            PublicationIoTarget.freeze(value)
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
    "FreezeRuntimeIdentity",
    "FreezeOperationResult",
]
