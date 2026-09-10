from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from trader.domain.research.history_control import (
    HistoryActiveSnapshot,
    HistoryCalendarIdentity,
    HistoryControlState,
    HistoryDiskRequirement,
    HistoryReminderState,
    HistorySecurityIdentity,
    HistorySnapshotPartition,
    HistorySourceIdentity,
    HistorySyncCheckpoint,
    HistoryTrainingDueState,
    HistoryUniverseIdentity,
)
from trader.infra.research.history_control_repository import (
    HistoryControlConflictError,
    HistoryControlRegressionError,
    SQLiteHistoryControlRepository,
    HistoryMaintenanceAlreadyRunningError,
    HistoryMaintenanceLock,
    inspect_history_disk,
)

NOW = datetime(2026, 9, 10, 20, 30, tzinfo=ZoneInfo("Asia/Shanghai"))


def _values(sequence: int = 1, cutoff: date = date(2026, 9, 10)):
    source = HistorySourceIdentity("baostock", "baostock_daily", "python-sdk", NOW)
    calendar = HistoryCalendarIdentity((date(2026, 9, 9), cutoff), source.content_hash)
    universe = HistoryUniverseIdentity(
        (HistorySecurityIdentity("600001", "浦发银行", "main", date(1999, 11, 10), None),),
        source.content_hash,
    )
    checkpoint = HistorySyncCheckpoint("sync-20260910", 1, "completed", NOW, 2, 2, None)
    due = HistoryTrainingDueState("due-20260910", "initial_training_required", None, cutoff, 0, False, NOW)
    reminder_at = NOW.replace(year=cutoff.year, month=cutoff.month, day=cutoff.day)
    reminder = HistoryReminderState("due-20260910", cutoff, "notification_degraded", reminder_at, "notify_failed")
    snapshot = HistoryActiveSnapshot(
        sequence,
        cutoff,
        cutoff,
        calendar.content_hash,
        universe.content_hash,
        source.content_hash,
        (HistorySnapshotPartition(f"partitions/{cutoff:%Y/%m}.sqlite3", "a" * 64, 2),),
    )
    return source, calendar, universe, checkpoint, due, reminder, snapshot


def _save_all(repository: SQLiteHistoryControlRepository, values) -> HistoryControlState:
    source, calendar, universe, checkpoint, due, reminder, snapshot = values
    repository.save_source(source)
    repository.save_calendar(calendar)
    repository.save_universe(universe)
    repository.save_checkpoint(checkpoint)
    repository.save_due_state(due)
    repository.save_reminder(reminder)
    repository.publish_snapshot(snapshot)
    return repository.load_state()


def test_control_repository_round_trips_typed_state_and_replays_same_content(tmp_path: Path) -> None:
    repository = SQLiteHistoryControlRepository(tmp_path / "control.sqlite3")
    repository.initialize()
    values = _values()

    expected = _save_all(repository, values)
    replayed = _save_all(repository, values)

    assert replayed == expected
    assert replayed.active_snapshot == values[-1]
    assert repository.integrity().state == "healthy"


def test_control_repository_rejects_same_identity_with_different_content(tmp_path: Path) -> None:
    repository = SQLiteHistoryControlRepository(tmp_path / "control.sqlite3")
    repository.initialize()
    values = _values()
    repository.save_checkpoint(values[3])

    conflicting = replace(values[3], state="failed", completed_units=1, error_code="supplier_failed")
    with pytest.raises(HistoryControlConflictError, match="immutable"):
        repository.save_checkpoint(conflicting)


def test_snapshot_publication_recovers_after_interruption_without_pointer_regression(tmp_path: Path) -> None:
    path = tmp_path / "control.sqlite3"
    first_repository = SQLiteHistoryControlRepository(path)
    first_repository.initialize()
    first = _values()
    _save_all(first_repository, first)
    second_values = _values(2, date(2026, 9, 11))
    second = second_values[-1]
    interrupted = SQLiteHistoryControlRepository(path)
    interrupted.save_calendar(second_values[1])
    interrupted.save_universe(second_values[2])

    def interrupt(stage: str) -> None:
        if stage == "snapshot_committed":
            raise RuntimeError("simulated power loss")

    interrupted = SQLiteHistoryControlRepository(path, fault_injector=interrupt)
    with pytest.raises(RuntimeError, match="power loss"):
        interrupted.publish_snapshot(second)
    assert interrupted.load_state().active_snapshot == first[-1]

    recovered = SQLiteHistoryControlRepository(path)
    recovered.publish_snapshot(second)
    assert recovered.load_state().active_snapshot == second
    with pytest.raises(HistoryControlRegressionError, match="regress"):
        recovered.publish_snapshot(first[-1])


def test_snapshot_publication_rejects_unsealed_parent_identities(tmp_path: Path) -> None:
    repository = SQLiteHistoryControlRepository(tmp_path / "control.sqlite3")
    repository.initialize()
    snapshot = _values()[-1]

    with pytest.raises(HistoryControlConflictError, match="parent identities"):
        repository.publish_snapshot(snapshot)


def test_corrupt_control_database_rebuilds_from_verified_typed_state(tmp_path: Path) -> None:
    path = tmp_path / "control.sqlite3"
    repository = SQLiteHistoryControlRepository(path)
    repository.initialize()
    expected = _save_all(repository, _values())
    for candidate in (Path(f"{path}-wal"), Path(f"{path}-shm")):
        candidate.unlink(missing_ok=True)
    corrupt_bytes = b"corrupt-control-database"
    path.write_bytes(corrupt_bytes)

    assert repository.integrity().state == "corrupt"

    def interrupt(stage: str) -> None:
        if stage == "rebuild_ready":
            raise RuntimeError("simulated rebuild interruption")

    with pytest.raises(RuntimeError, match="rebuild interruption"):
        SQLiteHistoryControlRepository.rebuild(path, expected, fault_injector=interrupt)
    assert path.read_bytes() == corrupt_bytes

    SQLiteHistoryControlRepository.rebuild(path, expected)

    rebuilt = SQLiteHistoryControlRepository(path)
    assert rebuilt.integrity().state == "healthy"
    assert rebuilt.load_state() == expected


def test_history_lock_and_disk_preflight_are_bounded_and_typed(tmp_path: Path) -> None:
    first = HistoryMaintenanceLock(tmp_path / "history-maintenance.lock")
    second = HistoryMaintenanceLock(tmp_path / "history-maintenance.lock")
    first.acquire()
    try:
        with pytest.raises(HistoryMaintenanceAlreadyRunningError, match="already running"):
            second.acquire()
    finally:
        first.release()

    requirement = HistoryDiskRequirement(100, 20, 30, 10)
    sufficient = inspect_history_disk(
        tmp_path / "missing" / "archive",
        requirement,
        disk_usage=lambda _path: SimpleNamespace(free=160),
    )
    insufficient = inspect_history_disk(
        tmp_path,
        requirement,
        disk_usage=lambda _path: SimpleNamespace(free=159),
    )
    assert sufficient.sufficient is True
    assert sufficient.required_free_bytes == 160
    assert insufficient.sufficient is False
