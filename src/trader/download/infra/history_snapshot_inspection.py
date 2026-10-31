"""Read-only inspection of the published monthly history snapshot."""

from __future__ import annotations

from trader.infra.sqlite_policy import SqliteIoPolicy

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from trader.download.domain.history_control import (
    HistoryCalendarIdentity,
    HistorySnapshot,
    HistorySourceIdentity,
    HistoryUniverseIdentity,
)
from trader.download.domain.history_snapshot_status import HistorySnapshotStatus
from trader.download.domain.prepared_reference import PreparedReference
from trader.download.infra.history_control_index import HistoryControlError, HistoryControlIndex
from trader.download.infra.history_prices import (
    HistoryPricePartition,
    HistoryPricePartitionError,
)
from trader.download.infra.history_reference_index import ReadBoundHistoryReference


class HistorySnapshotInspectionError(RuntimeError):
    """The published monthly snapshot cannot be trusted."""


@dataclass(frozen=True)
class PublishedHistorySnapshot:
    root: Path
    snapshot: HistorySnapshot
    source: HistorySourceIdentity
    calendar: HistoryCalendarIdentity
    universe: HistoryUniverseIdentity


def InspectHistorySnapshot(root: Path, *, verify_partitions: bool = False, sqlite_policy: SqliteIoPolicy) -> HistorySnapshotStatus:
    history_root = root
    if not (history_root / "catalog.sqlite3").is_file():
        return _unavailable()
    try:
        history = LoadPublishedHistorySnapshot(history_root, sqlite_policy=sqlite_policy)
    except HistorySnapshotInspectionError as exc:
        reason = str(exc)
        return _unavailable() if reason == "history_snapshot_unavailable" else _invalid(reason)
    if verify_partitions:
        try:
            VerifyPublishedHistorySnapshot(history, sqlite_policy=sqlite_policy)
        except HistorySnapshotInspectionError as exc:
            return _status(history, "invalid", str(exc))
    return _status(history, "published", None)


def LoadPublishedHistorySnapshot(root: Path, *, sequence: int | None = None, sqlite_policy: SqliteIoPolicy) -> PublishedHistorySnapshot:
    history_root = root
    control_path = history_root / "catalog.sqlite3"
    if not control_path.is_file():
        raise HistorySnapshotInspectionError("history_snapshot_unavailable")
    try:
        references: dict[str, PreparedReference] = {}
        state = HistoryControlIndex(control_path, sqlite_policy=sqlite_policy).load_published_state(sequence=sequence, references=references)
    except HistoryControlError as exc:
        raise HistorySnapshotInspectionError("history_control_invalid") from exc
    if state is None:
        raise HistorySnapshotInspectionError("history_snapshot_unavailable")
    try:
        reference = ReadBoundHistoryReference(history_root, state.source.supplier_contract, references=references, sqlite_policy=sqlite_policy)
        if set(reference.eligible_codes) != {item.code for item in state.universe.securities}:
            raise ValueError("history reference universe conflict")
    except (OSError, ValueError) as exc:
        raise HistorySnapshotInspectionError("history_reference_unavailable") from exc
    return PublishedHistorySnapshot(history_root, state.snapshot, state.source, state.calendar, state.universe)


def VerifyPublishedHistorySnapshot(history: PublishedHistorySnapshot, *, sqlite_policy: SqliteIoPolicy) -> None:
    try:
        ReadBoundHistoryReference(history.root, history.source.supplier_contract, sqlite_policy=sqlite_policy)
    except (OSError, ValueError) as exc:
        raise HistorySnapshotInspectionError("history_reference_unavailable") from exc
    try:
        for reference in history.snapshot.partitions:
            HistoryPricePartition.verify(history.root / reference.relative_path, reference, sqlite_policy=sqlite_policy)
    except (HistoryPricePartitionError, OSError, ValueError) as exc:
        raise HistorySnapshotInspectionError("history_snapshot_partition_invalid") from exc


def _unavailable() -> HistorySnapshotStatus:
    return HistorySnapshotStatus("unavailable", None, None, None, 0, 0, 0, "history_snapshot_unavailable")


def _invalid(reason: str) -> HistorySnapshotStatus:
    return HistorySnapshotStatus("invalid", None, None, None, 0, 0, 0, reason)


def _status(
    history: PublishedHistorySnapshot,
    state: Literal["published", "invalid"],
    reason: str | None,
) -> HistorySnapshotStatus:
    snapshot = history.snapshot
    return HistorySnapshotStatus(
        state,
        snapshot.content_hash,
        snapshot.data_cutoff,
        snapshot.label_cutoff,
        len(history.calendar.open_dates),
        len(history.universe.securities),
        len(snapshot.partitions),
        reason,
    )


__all__ = [
    "PublishedHistorySnapshot",
    "HistorySnapshotInspectionError",
    "InspectHistorySnapshot",
    "LoadPublishedHistorySnapshot",
    "VerifyPublishedHistorySnapshot",
]
