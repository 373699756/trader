"""Formal scored-freeze persistence boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol

from trader.recommendation.domain.publication.freeze_identity import CommittedFreezeRecord
from trader.recommendation.domain.publication.models import Strategy


class FreezeRecordError(RuntimeError):
    """Base failure for immutable freeze records."""


class FreezeRecordConflictError(FreezeRecordError):
    """A strategy/date key already contains different immutable content."""


class FreezeRecordUnavailableError(FreezeRecordError):
    """A persistence or verification failure prevented a trusted read/write."""


@dataclass(frozen=True)
class FreezeRecordRecoverySummary:
    recovered: int = 0
    quarantined: int = 0
    orphaned: int = 0


class FreezeRecordPort(Protocol):
    def initialize(self) -> None: ...

    def commit(self, record: CommittedFreezeRecord) -> None: ...

    def load(self, strategy: Strategy, trade_date: date) -> CommittedFreezeRecord | None: ...

    def list_dates(self, strategy: Strategy, *, limit: int = 31) -> tuple[date, ...]: ...

    def recover(self) -> FreezeRecordRecoverySummary: ...


__all__ = [
    "FreezeRecordConflictError",
    "FreezeRecordError",
    "FreezeRecordRecoverySummary",
    "FreezeRecordPort",
    "FreezeRecordUnavailableError",
]
