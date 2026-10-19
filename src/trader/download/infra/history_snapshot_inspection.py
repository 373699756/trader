"""Read-only inspection of the active monthly history snapshot."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from trader.download.domain.history_control import (
    HistoryActiveSnapshot,
    HistoryCalendarIdentity,
    HistorySourceIdentity,
    HistoryUniverseIdentity,
)
from trader.download.domain.history_snapshot_status import HistorySnapshotStatus
from trader.download.infra.history_control_index import HistoryControlError, HistoryControlIndex
from trader.download.infra.history_prices import (
    HistoryPricePartition,
    HistoryPricePartitionError,
)
from trader.download.infra.history_reference_codec import ReadBoundHistoryReference


class HistorySnapshotInspectionError(RuntimeError):
    """The active monthly snapshot cannot be trusted."""


@dataclass(frozen=True)
class ActiveHistorySnapshot:
    root: Path
    snapshot: HistoryActiveSnapshot
    source: HistorySourceIdentity
    calendar: HistoryCalendarIdentity
    universe: HistoryUniverseIdentity


def InspectHistorySnapshot(root: Path, *, verify_partitions: bool = False) -> HistorySnapshotStatus:
    history_root = root
    if not (history_root / "catalog.sqlite3").is_file():
        return _unavailable()
    try:
        active = LoadActiveHistorySnapshot(history_root)
    except HistorySnapshotInspectionError as exc:
        reason = str(exc)
        return _unavailable() if reason == "history_snapshot_unavailable" else _invalid(reason)
    if verify_partitions:
        try:
            VerifyActiveHistorySnapshot(active)
        except HistorySnapshotInspectionError as exc:
            return _status(active, "invalid", str(exc))
    return _status(active, "active", None)


def LoadActiveHistorySnapshot(root: Path) -> ActiveHistorySnapshot:
    history_root = root
    control_path = history_root / "catalog.sqlite3"
    if not control_path.is_file():
        raise HistorySnapshotInspectionError("history_snapshot_unavailable")
    try:
        state = HistoryControlIndex(control_path).load_published_state()
    except HistoryControlError as exc:
        raise HistorySnapshotInspectionError("history_control_invalid") from exc
    if state is None:
        raise HistorySnapshotInspectionError("history_snapshot_unavailable")
    try:
        reference = ReadBoundHistoryReference(history_root, state.source.supplier_contract)
        if set(reference.eligible_codes) != {item.code for item in state.universe.securities}:
            raise ValueError("history reference universe conflict")
    except (OSError, ValueError) as exc:
        raise HistorySnapshotInspectionError("history_reference_unavailable") from exc
    return ActiveHistorySnapshot(history_root, state.snapshot, state.source, state.calendar, state.universe)


def VerifyActiveHistorySnapshot(active: ActiveHistorySnapshot) -> None:
    try:
        ReadBoundHistoryReference(active.root, active.source.supplier_contract)
    except (OSError, ValueError) as exc:
        raise HistorySnapshotInspectionError("history_reference_unavailable") from exc
    try:
        for reference in active.snapshot.partitions:
            HistoryPricePartition.verify(active.root / reference.relative_path, reference)
    except (HistoryPricePartitionError, OSError, ValueError) as exc:
        raise HistorySnapshotInspectionError("history_snapshot_partition_invalid") from exc


def _unavailable() -> HistorySnapshotStatus:
    return HistorySnapshotStatus("unavailable", None, None, None, 0, 0, 0, "history_snapshot_unavailable")


def _invalid(reason: str) -> HistorySnapshotStatus:
    return HistorySnapshotStatus("invalid", None, None, None, 0, 0, 0, reason)


def _status(
    active: ActiveHistorySnapshot,
    state: Literal["active", "invalid"],
    reason: str | None,
) -> HistorySnapshotStatus:
    snapshot = active.snapshot
    return HistorySnapshotStatus(
        state,
        snapshot.content_hash,
        snapshot.data_cutoff,
        snapshot.label_cutoff,
        len(active.calendar.open_dates),
        len(active.universe.securities),
        len(snapshot.partitions),
        reason,
    )


__all__ = [
    "ActiveHistorySnapshot",
    "HistorySnapshotInspectionError",
    "InspectHistorySnapshot",
    "LoadActiveHistorySnapshot",
    "VerifyActiveHistorySnapshot",
]
