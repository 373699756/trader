"""Training input adapter for the stable-path monthly history archive."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

from trader.domain.research.baostock_daily import (
    BAOSTOCK_RESEARCH_IDENTITY,
    BaoStockCalendar,
    BaoStockSecurity,
    BaoStockTrainingRow,
)
from trader.domain.research.history_control import (
    HistoryActiveSnapshot,
    HistorySecurityIdentity,
)
from trader.domain.research.tomorrow_training_input import (
    REQUIRED_DAILY_FIELDS,
    FrozenDailyInputDescriptor,
)
from trader.infra.research.history_control_repository import (
    HistoryControlError,
    SQLiteHistoryControlRepository,
)
from trader.infra.research.history_month_archive import (
    HistoryMonthlyArchiveError,
    SQLiteHistoryMonthlyArchive,
)


@dataclass(frozen=True)
class HistoryTrainingInputSnapshot:
    input_scope: Literal["complete_manifest"]
    active_snapshot_hash: str
    source_identity_hash: str
    calendar_hash: str
    source_cutoff: date
    label_cutoff: date
    calendar: BaoStockCalendar
    training_codes: tuple[str, ...]
    input_descriptor_hash: str

    def __post_init__(self) -> None:
        hashes = (
            self.active_snapshot_hash,
            self.source_identity_hash,
            self.calendar_hash,
            self.input_descriptor_hash,
        )
        dates = self.calendar.open_dates
        if (
            any(len(value) != 64 or any(character not in "0123456789abcdef" for character in value) for value in hashes)
            or not dates
            or self.source_cutoff != dates[-1]
            or self.label_cutoff not in dates
            or self.label_cutoff > self.source_cutoff
            or not self.training_codes
            or self.training_codes != tuple(sorted(set(self.training_codes)))
            or any(len(code) != 6 or not code.isdigit() for code in self.training_codes)
        ):
            raise ValueError("history training input snapshot is invalid")

    @property
    def universe_count(self) -> int:
        return len(self.training_codes)


class HistoryTrainingInputError(RuntimeError):
    """The active monthly snapshot cannot be used as a complete input."""


class SQLiteHistoryTrainingInputArchive:
    """Expose one verified active snapshot through the V3 training row contract."""

    def __init__(
        self,
        root: Path,
        active: HistoryActiveSnapshot,
        calendar: BaoStockCalendar,
        universe: tuple[BaoStockSecurity, ...],
        descriptor: FrozenDailyInputDescriptor,
    ) -> None:
        self._root = root
        self._active = active
        self._calendar = calendar
        self._universe = universe
        self._archive = SQLiteHistoryMonthlyArchive(root)
        self._codes = frozenset(item.code for item in universe)
        self._descriptor = descriptor
        self.snapshot = _snapshot_for_training(active, calendar, self._codes, descriptor)

    @classmethod
    def open(cls, root: Path) -> SQLiteHistoryTrainingInputArchive:
        archive_root = root / "baostock" if (root / "baostock").is_dir() else root
        control = SQLiteHistoryControlRepository(archive_root / "control.sqlite3")
        try:
            state = control.load_state()
        except HistoryControlError as exc:
            raise HistoryTrainingInputError("history_manifest_unavailable") from exc
        active = state.active_snapshot
        if active is None:
            raise HistoryTrainingInputError("history_manifest_unavailable")
        calendar_identity = next(
            (item for item in state.calendars if item.content_hash == active.calendar_hash),
            None,
        )
        universe_identity = next(
            (item for item in state.universes if item.content_hash == active.universe_hash),
            None,
        )
        if calendar_identity is None or universe_identity is None:
            raise HistoryTrainingInputError("history_manifest_parent_unavailable")
        calendar = BaoStockCalendar(calendar_identity.open_dates)
        universe = tuple(_security(item) for item in universe_identity.securities)
        descriptor = FrozenDailyInputDescriptor(
            manifest_hash=active.content_hash,
            source_identity=BAOSTOCK_RESEARCH_IDENTITY,
            source_cutoff=active.data_cutoff,
            requested_sessions=len(calendar.open_dates),
            primary_key=("code", "trade_date"),
            fields=REQUIRED_DAILY_FIELDS,
            raw_qfq_layout="same_row",
            row_hash_algorithm="sha256",
            frozen=True,
            production_authority=False,
        )
        return cls(archive_root, active, calendar, universe, descriptor)

    @property
    def active_snapshot(self) -> HistoryActiveSnapshot:
        return self._active

    @property
    def archive_root(self) -> Path:
        return self._root

    def describe_frozen_daily_input(self) -> FrozenDailyInputDescriptor:
        return self._descriptor

    def read_training_rows(
        self,
        code: str,
        *,
        allowed_dates: frozenset[date],
    ) -> tuple[BaoStockTrainingRow, ...]:
        if code not in self._codes:
            raise HistoryTrainingInputError("history_code_outside_active_universe")
        try:
            rows = (
                revision.training_row
                for revision in self._archive.iter_code(
                    code,
                    min(allowed_dates),
                    max(allowed_dates),
                    self._active,
                )
                if revision.trade_date in allowed_dates
            )
            return tuple(sorted((row for row in rows if row is not None), key=lambda item: item.trade_date))
        except (HistoryMonthlyArchiveError, OSError, ValueError) as exc:
            raise HistoryTrainingInputError("history_snapshot_unavailable") from exc


def _security(value: HistorySecurityIdentity) -> BaoStockSecurity:
    return BaoStockSecurity(
        value.code,
        value.name,
        value.board,
        value.listed_on,
        value.delisted_on,
        "history-control",
    )


def _snapshot_for_training(
    active: HistoryActiveSnapshot,
    calendar: BaoStockCalendar,
    codes: frozenset[str],
    descriptor: FrozenDailyInputDescriptor,
) -> HistoryTrainingInputSnapshot:
    return HistoryTrainingInputSnapshot(
        "complete_manifest",
        active.content_hash,
        active.source_identity_hash,
        active.calendar_hash,
        active.data_cutoff,
        active.label_cutoff,
        calendar,
        tuple(sorted(codes)),
        descriptor.content_hash,
    )


__all__ = [
    "HistoryTrainingInputError",
    "HistoryTrainingInputSnapshot",
    "SQLiteHistoryTrainingInputArchive",
]
