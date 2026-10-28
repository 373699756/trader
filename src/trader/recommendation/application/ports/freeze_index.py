"""Application port for freeze coordinators to interact with the freeze index."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Protocol

from trader.recommendation.domain.publication.freeze_identity import (
    CommittedFreezeRecord,
    FreezeIdentity,
    ScoredFreeze,
)
from trader.recommendation.domain.publication.models import Strategy


class FreezeSnapshotPort(Protocol):
    @property
    def current(self) -> FreezeIdentity | None: ...


class FreezeSealPort(Protocol):
    @property
    def accepted(self) -> bool: ...

    @property
    def reason(self) -> str: ...

    @property
    def freeze(self) -> ScoredFreeze | None: ...

    @property
    def source(self) -> Literal["current", "explicit"] | None: ...


class FreezeIndexPort(Protocol):
    def snapshot(self, strategy: Strategy) -> FreezeSnapshotPort: ...

    def is_sealed(self, strategy: Strategy, trade_date: date) -> bool: ...

    def is_closed(self, strategy: Strategy, trade_date: date) -> bool: ...

    def close_for_date(self, strategy: Strategy, trade_date: date, *, boundary_at: datetime) -> bool: ...

    def discard_closed_current(self, strategy: Strategy, trade_date: date) -> bool: ...

    def seal_for_freeze(
        self,
        strategy: Strategy,
        *,
        boundary_at: datetime,
    ) -> FreezeSealPort: ...

    def seal_close_fallback(
        self,
        freeze: ScoredFreeze,
        *,
        boundary_at: datetime,
        official_close_version: str,
    ) -> FreezeSealPort: ...

    def commit_formal(self, record: CommittedFreezeRecord) -> bool: ...

    def restore_formal(self, record: CommittedFreezeRecord) -> bool: ...


__all__ = ["FreezeIndexPort", "FreezeSealPort", "FreezeSnapshotPort"]
