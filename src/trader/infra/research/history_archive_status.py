"""Read-only inspection of the active stable-path monthly archive."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from trader.application.research.history_archive_status import HistoryArchiveStatus
from trader.domain.research.history_control import (
    HistoryActiveSnapshot,
    HistoryCalendarIdentity,
    HistorySourceIdentity,
    HistoryUniverseIdentity,
)
from trader.infra.research.history_control_repository import HistoryControlError, SQLiteHistoryControlRepository
from trader.infra.research.history_month_partition import (
    HistoryMonthPartitionError,
    SQLiteHistoryMonthPartitionRepository,
)


class HistoryArchiveError(RuntimeError):
    """The active monthly archive cannot be trusted."""


@dataclass(frozen=True)
class ActiveHistoryArchive:
    root: Path
    snapshot: HistoryActiveSnapshot
    source: HistorySourceIdentity
    calendar: HistoryCalendarIdentity
    universe: HistoryUniverseIdentity


def inspect_history_archive(root: Path, *, verify_partitions: bool = False) -> HistoryArchiveStatus:
    archive_root = _archive_root(root)
    if not (archive_root / "control.sqlite3").is_file():
        return _unavailable()
    try:
        archive = load_active_history_archive(archive_root)
    except HistoryArchiveError as exc:
        reason = str(exc)
        return _unavailable() if reason == "history_snapshot_unavailable" else _invalid(reason)
    if verify_partitions:
        try:
            verify_active_history_archive(archive)
        except HistoryArchiveError as exc:
            return _status(archive, "invalid", str(exc))
    return _status(archive, "active", None)


def load_active_history_archive(root: Path) -> ActiveHistoryArchive:
    archive_root = _archive_root(root)
    control_path = archive_root / "control.sqlite3"
    if not control_path.is_file():
        raise HistoryArchiveError("history_snapshot_unavailable")
    try:
        state = SQLiteHistoryControlRepository(control_path).load_state()
    except HistoryControlError as exc:
        raise HistoryArchiveError("history_control_invalid") from exc
    active = state.active_snapshot
    if active is None:
        raise HistoryArchiveError("history_snapshot_unavailable")
    source = next((item for item in state.sources if item.content_hash == active.source_identity_hash), None)
    calendar = next((item for item in state.calendars if item.content_hash == active.calendar_hash), None)
    universe = next((item for item in state.universes if item.content_hash == active.universe_hash), None)
    if source is None or calendar is None or universe is None:
        raise HistoryArchiveError("history_snapshot_parent_invalid")
    if calendar.open_dates[-1] != active.data_cutoff or active.label_cutoff not in calendar.open_dates:
        raise HistoryArchiveError("history_snapshot_parent_invalid")
    return ActiveHistoryArchive(archive_root, active, source, calendar, universe)


def verify_active_history_archive(archive: ActiveHistoryArchive) -> None:
    try:
        for reference in archive.snapshot.partitions:
            SQLiteHistoryMonthPartitionRepository.verify(archive.root / reference.relative_path, reference)
    except (HistoryMonthPartitionError, OSError, ValueError) as exc:
        raise HistoryArchiveError("history_snapshot_partition_invalid") from exc


def _archive_root(root: Path) -> Path:
    return root if root.name == "baostock" else root / "baostock"


def _unavailable() -> HistoryArchiveStatus:
    return HistoryArchiveStatus("unavailable", None, None, None, 0, 0, 0, "history_snapshot_unavailable")


def _invalid(reason: str) -> HistoryArchiveStatus:
    return HistoryArchiveStatus("invalid", None, None, None, 0, 0, 0, reason)


def _status(
    archive: ActiveHistoryArchive,
    state: Literal["active", "invalid"],
    reason: str | None,
) -> HistoryArchiveStatus:
    snapshot = archive.snapshot
    return HistoryArchiveStatus(
        state,
        snapshot.content_hash,
        snapshot.data_cutoff,
        snapshot.label_cutoff,
        len(archive.calendar.open_dates),
        len(archive.universe.securities),
        len(snapshot.partitions),
        reason,
    )


__all__ = [
    "ActiveHistoryArchive",
    "HistoryArchiveError",
    "inspect_history_archive",
    "load_active_history_archive",
    "verify_active_history_archive",
]
