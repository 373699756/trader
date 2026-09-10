"""Immutable identities owned by the zero-argument history control plane."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import PurePosixPath
from typing import Literal
from zoneinfo import ZoneInfo

from trader.domain.research.h1_point_in_time import canonical_hash

HistorySecurityBoard = Literal["main", "growth", "star"]
HistorySyncState = Literal["pending", "running", "completed", "failed", "cancelled"]
HistoryTrainingDueReason = Literal[
    "not_due",
    "initial_training_required",
    "cadence_due",
    "input_revision_due",
    "data_incomplete",
]
HistoryReminderOutcome = Literal["sent", "notification_degraded"]

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDENTITY = re.compile(r"^[a-z0-9_.:-]{1,128}$")
_CODE = re.compile(r"^[0-9]{6}$")
_ERROR_CODE = re.compile(r"^[a-z0-9_]{1,64}$")
_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _require_hash(value: str, label: str) -> None:
    if _SHA256.fullmatch(value) is None:
        raise ValueError(f"history {label} must be SHA-256")


def _require_identity(value: str, label: str) -> None:
    if _IDENTITY.fullmatch(value) is None:
        raise ValueError(f"history {label} is invalid")


def _require_shanghai(value: datetime, label: str) -> None:
    if value.tzinfo != _SHANGHAI:
        raise ValueError(f"history {label} must use Asia/Shanghai")


@dataclass(frozen=True)
class HistorySourceIdentity:
    source: str
    dataset: str
    supplier_contract: str
    observed_at: datetime
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        for value, label in (
            (self.source, "source"),
            (self.dataset, "dataset"),
            (self.supplier_contract, "supplier contract"),
        ):
            _require_identity(value, label)
        _require_shanghai(self.observed_at, "source observation")
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True, order=True)
class HistorySecurityIdentity:
    code: str
    name: str
    board: HistorySecurityBoard
    listed_on: date
    delisted_on: date | None

    def __post_init__(self) -> None:
        if (
            _CODE.fullmatch(self.code) is None
            or not self.name.strip()
            or self.board not in {"main", "growth", "star"}
            or (self.delisted_on is not None and self.delisted_on <= self.listed_on)
        ):
            raise ValueError("history security identity is invalid")


@dataclass(frozen=True)
class HistoryCalendarIdentity:
    open_dates: tuple[date, ...]
    source_identity_hash: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        dates = tuple(self.open_dates)
        _require_hash(self.source_identity_hash, "calendar source identity")
        if not dates or len(dates) > 2000 or dates != tuple(sorted(set(dates))):
            raise ValueError("history calendar identity is invalid")
        object.__setattr__(self, "open_dates", dates)
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class HistoryUniverseIdentity:
    securities: tuple[HistorySecurityIdentity, ...]
    source_identity_hash: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        securities = tuple(sorted(self.securities, key=lambda item: item.code))
        _require_hash(self.source_identity_hash, "universe source identity")
        if not securities or len({item.code for item in securities}) != len(securities):
            raise ValueError("history universe identity is invalid")
        object.__setattr__(self, "securities", securities)
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class HistorySyncCheckpoint:
    sync_identity: str
    ordinal: int
    state: HistorySyncState
    observed_at: datetime
    completed_units: int
    total_units: int
    error_code: str | None
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_identity(self.sync_identity, "sync identity")
        _require_shanghai(self.observed_at, "checkpoint observation")
        valid_error = self.error_code is None or _ERROR_CODE.fullmatch(self.error_code) is not None
        terminal_valid = (
            self.state == "completed"
            and self.completed_units == self.total_units
            and self.error_code is None
            or self.state == "failed"
            and self.error_code is not None
            or self.state in {"pending", "running"}
            and self.error_code is None
            or self.state == "cancelled"
        )
        if (
            self.ordinal < 1
            or self.state not in {"pending", "running", "completed", "failed", "cancelled"}
            or self.total_units < 0
            or not 0 <= self.completed_units <= self.total_units
            or not valid_error
            or not terminal_valid
        ):
            raise ValueError("history sync checkpoint is invalid")
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class HistoryTrainingDueState:
    due_identity: str
    reason: HistoryTrainingDueReason
    baseline_label_cutoff: date | None
    current_label_cutoff: date | None
    matured_label_days_since_training: int
    input_revision: bool
    observed_at: datetime
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_identity(self.due_identity, "training due identity")
        _require_shanghai(self.observed_at, "training due observation")
        valid = (
            self.reason == "initial_training_required"
            and self.baseline_label_cutoff is None
            and self.current_label_cutoff is not None
            or self.reason == "cadence_due"
            and self.baseline_label_cutoff is not None
            and self.current_label_cutoff is not None
            and self.matured_label_days_since_training >= 20
            or self.reason == "input_revision_due"
            and self.input_revision
            or self.reason == "not_due"
            and not self.input_revision
            and self.matured_label_days_since_training < 20
            or self.reason == "data_incomplete"
        )
        if self.matured_label_days_since_training < 0 or not valid:
            raise ValueError("history training due state is invalid")
        object.__setattr__(self, "content_hash", canonical_hash(self))

    @property
    def training_due(self) -> bool:
        return self.reason in {"initial_training_required", "cadence_due", "input_revision_due"}


@dataclass(frozen=True)
class HistoryReminderState:
    due_identity: str
    reminder_date: date
    outcome: HistoryReminderOutcome
    attempted_at: datetime
    error_code: str | None
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_identity(self.due_identity, "reminder due identity")
        _require_shanghai(self.attempted_at, "reminder attempt")
        valid = (
            self.outcome == "sent"
            and self.error_code is None
            or self.outcome == "notification_degraded"
            and self.error_code is not None
            and _ERROR_CODE.fullmatch(self.error_code) is not None
        )
        if not valid or self.attempted_at.date() != self.reminder_date:
            raise ValueError("history reminder state is invalid")
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True, order=True)
class HistorySnapshotPartition:
    relative_path: str
    sha256: str
    row_count: int

    def __post_init__(self) -> None:
        path = PurePosixPath(self.relative_path)
        parts = path.parts
        _require_hash(self.sha256, "partition")
        if (
            path.is_absolute()
            or len(parts) != 3
            or parts[0] != "partitions"
            or len(parts[1]) != 4
            or not parts[1].isdigit()
            or len(path.stem) != 2
            or not path.stem.isdigit()
            or path.suffix != ".sqlite3"
            or not 1 <= int(path.stem) <= 12
            or self.row_count < 0
        ):
            raise ValueError("history snapshot partition is invalid")


@dataclass(frozen=True)
class HistoryActiveSnapshot:
    sequence: int
    data_cutoff: date
    label_cutoff: date
    calendar_hash: str
    universe_hash: str
    source_identity_hash: str
    partitions: tuple[HistorySnapshotPartition, ...]
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        for value, label in (
            (self.calendar_hash, "snapshot calendar"),
            (self.universe_hash, "snapshot universe"),
            (self.source_identity_hash, "snapshot source identity"),
        ):
            _require_hash(value, label)
        partitions = tuple(sorted(self.partitions))
        if (
            self.sequence < 1
            or self.label_cutoff > self.data_cutoff
            or not partitions
            or len({item.relative_path for item in partitions}) != len(partitions)
        ):
            raise ValueError("history active snapshot is invalid")
        object.__setattr__(self, "partitions", partitions)
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class HistoryDiskRequirement:
    month_sidecar_bytes: int
    sqlite_temporary_bytes: int
    training_workspace_bytes: int
    reserve_bytes: int

    def __post_init__(self) -> None:
        if any(value < 0 for value in self.components):
            raise ValueError("history disk requirement is invalid")

    @property
    def components(self) -> tuple[int, int, int, int]:
        return (
            self.month_sidecar_bytes,
            self.sqlite_temporary_bytes,
            self.training_workspace_bytes,
            self.reserve_bytes,
        )

    @property
    def required_free_bytes(self) -> int:
        return sum(self.components)


@dataclass(frozen=True)
class HistoryControlState:
    sources: tuple[HistorySourceIdentity, ...]
    calendars: tuple[HistoryCalendarIdentity, ...]
    universes: tuple[HistoryUniverseIdentity, ...]
    checkpoints: tuple[HistorySyncCheckpoint, ...]
    due_states: tuple[HistoryTrainingDueState, ...]
    reminders: tuple[HistoryReminderState, ...]
    snapshots: tuple[HistoryActiveSnapshot, ...]
    active_snapshot_hash: str | None

    def __post_init__(self) -> None:
        sources = tuple(sorted(self.sources, key=lambda item: item.content_hash))
        calendars = tuple(sorted(self.calendars, key=lambda item: item.content_hash))
        universes = tuple(sorted(self.universes, key=lambda item: item.content_hash))
        checkpoints = tuple(sorted(self.checkpoints, key=lambda item: (item.sync_identity, item.ordinal)))
        due_states = tuple(sorted(self.due_states, key=lambda item: item.due_identity))
        reminders = tuple(sorted(self.reminders, key=lambda item: (item.due_identity, item.reminder_date)))
        snapshots = tuple(sorted(self.snapshots, key=lambda item: item.sequence))
        if self.active_snapshot_hash is not None:
            _require_hash(self.active_snapshot_hash, "active snapshot")
        active = next((item for item in snapshots if item.content_hash == self.active_snapshot_hash), None)
        if self.active_snapshot_hash is not None and active is None:
            raise ValueError("history active snapshot identity is invalid")
        for values, identities in (
            (sources, tuple(item.content_hash for item in sources)),
            (calendars, tuple(item.content_hash for item in calendars)),
            (universes, tuple(item.content_hash for item in universes)),
            (checkpoints, tuple((item.sync_identity, item.ordinal) for item in checkpoints)),
            (due_states, tuple(item.due_identity for item in due_states)),
            (reminders, tuple((item.due_identity, item.reminder_date) for item in reminders)),
            (snapshots, tuple(item.sequence for item in snapshots)),
        ):
            if len(values) != len(set(identities)):
                raise ValueError("history control state contains duplicate identities")
        source_hashes = {item.content_hash for item in sources}
        calendar_hashes = {item.content_hash for item in calendars}
        universe_hashes = {item.content_hash for item in universes}
        if (
            any(item.source_identity_hash not in source_hashes for item in calendars)
            or any(item.source_identity_hash not in source_hashes for item in universes)
            or any(
                item.source_identity_hash not in source_hashes
                or item.calendar_hash not in calendar_hashes
                or item.universe_hash not in universe_hashes
                for item in snapshots
            )
        ):
            raise ValueError("history control state contains unsealed parent identities")
        object.__setattr__(self, "sources", sources)
        object.__setattr__(self, "calendars", calendars)
        object.__setattr__(self, "universes", universes)
        object.__setattr__(self, "checkpoints", checkpoints)
        object.__setattr__(self, "due_states", due_states)
        object.__setattr__(self, "reminders", reminders)
        object.__setattr__(self, "snapshots", snapshots)

    @property
    def active_snapshot(self) -> HistoryActiveSnapshot | None:
        return next((item for item in self.snapshots if item.content_hash == self.active_snapshot_hash), None)


__all__ = [
    "HistoryActiveSnapshot",
    "HistoryCalendarIdentity",
    "HistoryControlState",
    "HistoryDiskRequirement",
    "HistoryReminderOutcome",
    "HistoryReminderState",
    "HistorySecurityBoard",
    "HistorySecurityIdentity",
    "HistorySnapshotPartition",
    "HistorySourceIdentity",
    "HistorySyncCheckpoint",
    "HistorySyncState",
    "HistoryTrainingDueReason",
    "HistoryTrainingDueState",
    "HistoryUniverseIdentity",
]
