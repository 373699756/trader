from __future__ import annotations

import sqlite3
import subprocess
import sys
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import trader.training.infra.history.training_maintenance_index as maintenance_module
from tests.helpers.shared.configuration import SqlitePolicyFixture
from trader.infra.process_lock import ProcessLock
from trader.infra.sqlite_connection import SqliteConnection, SqliteSession, SqliteTransaction
from trader.training.domain.training_due import TrainingDueState
from trader.training.domain.training_reminder import TrainingReminderClaim, TrainingReminderState
from trader.training.infra.history.training_maintenance_index import (
    TrainingMaintenanceConflictError,
    TrainingMaintenanceError,
    TrainingMaintenanceIndex,
)

NOW = datetime(2026, 9, 10, 20, 30, tzinfo=ZoneInfo("Asia/Shanghai"))


def _records() -> tuple[TrainingDueState, TrainingReminderClaim, TrainingReminderState]:
    return (
        TrainingDueState("due-20260910", "initial_training_required", None, NOW.date(), 0, False, NOW),
        TrainingReminderClaim("due-20260910", NOW.date(), NOW),
        TrainingReminderState("due-20260910", NOW.date(), "notification_degraded", NOW, "notify_failed"),
    )


def _save_records(index: TrainingMaintenanceIndex) -> None:
    due, claim, reminder = _records()
    index.save_due_state(due)
    assert index.claim_reminder(claim) is True
    index.save_reminder(reminder)


def _committed_files(root: Path) -> dict[str, bytes]:
    return {
        file.name: file.read_bytes()
        for file in root.iterdir()
        if file.is_file() and not file.name.endswith("-shm")
        and not (file.name.endswith("-wal") and file.stat().st_size == 0)
    }


def test_missing_maintenance_reads_do_not_create_directories_or_database(tmp_path: Path) -> None:
    path = tmp_path / "missing" / "maintenance.sqlite3"
    index = TrainingMaintenanceIndex(path, sqlite_policy=SqlitePolicyFixture())

    assert index.read_due_state("due-20260910") is None
    state = index.load_state()

    assert (state.due_states, state.reminder_claims, state.reminders) == ((), (), ())
    assert not path.parent.exists()


def test_first_write_creates_current_schema_and_complete_transaction(tmp_path: Path) -> None:
    path = tmp_path / "training" / "maintenance.sqlite3"
    index = TrainingMaintenanceIndex(path, sqlite_policy=SqlitePolicyFixture())
    due = _records()[0]

    index.save_due_state(due)

    with SqliteSession(path) as connection:
        assert [tuple(row) for row in connection.run("SELECT * FROM schema_contract")] == [(1, "training_maintenance")]
        tables = {row[0] for row in connection.run("SELECT name FROM sqlite_master WHERE type='table'")}
        assert tables == {"schema_contract", "training_due", "training_reminder_claim", "training_reminder"}
    assert index.read_due_state(due.due_identity) == due
    assert not Path(f"{path}.sha256").exists()


def test_records_retain_logical_hashes_and_replay_without_rewriting(tmp_path: Path) -> None:
    path = tmp_path / "maintenance.sqlite3"
    index = TrainingMaintenanceIndex(path, sqlite_policy=SqlitePolicyFixture())
    due, claim, reminder = _records()
    _save_records(index)
    state = index.load_state()
    assert (
        state.due_states[0].content_hash,
        state.reminder_claims[0].content_hash,
        state.reminders[0].content_hash,
    ) == (
        "a00e3fbeb447e112089fe4c8ee37f54b4380395b4f585141a9d19e4a632df7d5",
        "73dfc48b74e90ecb37b7941178ab58fe4ed8833ad50ee40d6a8acd4bd1b03254",
        "f0206043102594ffb0c5e3041d60ce6a5994ba45bdec962ebcfb241325ebc9d5",
    )
    before = path.read_bytes()

    index.save_due_state(due)
    assert index.claim_reminder(claim) is False
    index.save_reminder(reminder)

    assert path.read_bytes() == before
    assert TrainingMaintenanceIndex(path, sqlite_policy=SqlitePolicyFixture()).load_state() == state


def test_conflicting_identity_does_not_replace_committed_due_or_reminder(tmp_path: Path) -> None:
    path = tmp_path / "maintenance.sqlite3"
    index = TrainingMaintenanceIndex(path, sqlite_policy=SqlitePolicyFixture())
    due, _claim, reminder = _records()
    _save_records(index)
    before = index.load_state(), path.read_bytes()

    with pytest.raises(TrainingMaintenanceConflictError):
        index.save_due_state(replace(due, input_revision=True))
    with pytest.raises(TrainingMaintenanceConflictError):
        index.save_reminder(replace(reminder, error_code="different_error"))

    assert (index.load_state(), path.read_bytes()) == before


def test_training_contract_due_round_trips_without_history_catalog(tmp_path: Path) -> None:
    path = tmp_path / "maintenance.sqlite3"
    due = TrainingDueState("due-contract", "training_contract_due", date(2026, 9, 9), NOW.date(), 1, False, NOW)

    TrainingMaintenanceIndex(path, sqlite_policy=SqlitePolicyFixture()).save_due_state(due)

    assert TrainingMaintenanceIndex(path, sqlite_policy=SqlitePolicyFixture()).load_state().due_states == (due,)
    assert not (tmp_path / "catalog.sqlite3").exists()


def test_reminder_claim_is_first_wins_across_restart_and_dates(tmp_path: Path) -> None:
    path = tmp_path / "maintenance.sqlite3"
    index = TrainingMaintenanceIndex(path, sqlite_policy=SqlitePolicyFixture())
    due, first, _reminder = _records()
    index.save_due_state(due)

    assert index.claim_reminder(first) is True
    restarted = TrainingMaintenanceIndex(path, sqlite_policy=SqlitePolicyFixture())
    assert restarted.claim_reminder(first) is False
    next_day = replace(first, reminder_date=date(2026, 9, 11), claimed_at=NOW.replace(day=11))
    assert restarted.claim_reminder(next_day) is True
    assert restarted.load_state().reminder_claims == (first, next_day)


def test_reminder_claim_is_committed_before_hard_exit_and_not_repeated(tmp_path: Path) -> None:
    path = tmp_path / "maintenance.sqlite3"
    index = TrainingMaintenanceIndex(path, sqlite_policy=SqlitePolicyFixture())
    due, claim, _reminder = _records()
    index.save_due_state(due)
    command = """
import os
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from tests.helpers.shared.configuration import SqlitePolicyFixture
from trader.training.domain.training_reminder import TrainingReminderClaim
from trader.training.infra.history.training_maintenance_index import TrainingMaintenanceIndex
index = TrainingMaintenanceIndex(Path(sys.argv[1]), sqlite_policy=SqlitePolicyFixture())
now = datetime(2026, 9, 10, 20, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
if not index.claim_reminder(TrainingReminderClaim("due-20260910", date(2026, 9, 10), now)):
    raise SystemExit(2)
os._exit(19)
"""

    completed = subprocess.run([sys.executable, "-c", command, str(path)], capture_output=True, text=True, timeout=15, check=False)

    assert completed.returncode == 19, completed.stderr
    restarted = TrainingMaintenanceIndex(path, sqlite_policy=SqlitePolicyFixture())
    assert restarted.load_state().reminder_claims == (claim,)
    assert restarted.claim_reminder(claim) is False


@pytest.mark.parametrize("initialized", (False, True))
def test_uncommitted_hard_exit_recovers_and_allows_the_same_due_to_retry(tmp_path: Path, initialized: bool) -> None:
    path = tmp_path / "maintenance.sqlite3"
    index = TrainingMaintenanceIndex(path, sqlite_policy=SqlitePolicyFixture())
    if initialized:
        _save_records(index)
        before = index.load_state()
    command = """
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from tests.helpers.shared.configuration import SqlitePolicyFixture
from trader.infra.sqlite_codec import EncodeDate, EncodeHash, EncodeTime
from trader.training.domain.training_due import TrainingDueState
from trader.training.infra.history.training_maintenance_index import TrainingMaintenanceIndex
index = TrainingMaintenanceIndex(Path(sys.argv[1]), sqlite_policy=SqlitePolicyFixture())
now = datetime(2026, 9, 10, 20, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
with index._connection(write=True) as connection:
    connection.run("PRAGMA cache_size=1")
    for number in range(2000):
        due = TrainingDueState(f"uncommitted-{number}", "initial_training_required", None, now.date(), 0, False, now)
        connection.run("INSERT INTO training_due VALUES(?,?,?,?,?,?,?,?)", (
            due.due_identity, due.reason, None, EncodeDate(now.date()), 0, 0, EncodeTime(now), EncodeHash(due.content_hash),
        ))
    os._exit(23)
"""

    completed = subprocess.run([sys.executable, "-c", command, str(path)], capture_output=True, text=True, timeout=20, check=False)

    assert completed.returncode == 23, completed.stderr
    journals = tuple(candidate for candidate in (Path(f"{path}-wal"), Path(f"{path}-journal")) if candidate.exists())
    assert journals and max(candidate.stat().st_size for candidate in journals) > 512
    restarted = TrainingMaintenanceIndex(path, sqlite_policy=SqlitePolicyFixture())
    if initialized:
        assert restarted.load_state() == before
        assert restarted.read_due_state("uncommitted-0") is None
    else:
        assert restarted.read_due_state("uncommitted-0") is None
        empty = restarted.load_state()
        assert (empty.due_states, empty.reminder_claims, empty.reminders) == ((), (), ())
    due = replace(_records()[0], due_identity="uncommitted-0")

    restarted.save_due_state(due)

    assert restarted.read_due_state(due.due_identity) == due
    assert len(restarted.load_state().due_states) == (2 if initialized else 1)


@pytest.mark.parametrize("initialized", (False, True))
def test_transaction_failure_rolls_back_and_same_record_can_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, initialized: bool) -> None:
    path = tmp_path / "maintenance.sqlite3"
    index = TrainingMaintenanceIndex(path, sqlite_policy=SqlitePolicyFixture())
    due = _records()[0]
    if initialized:
        _save_records(index)
        due = replace(due, due_identity="due-new")
    original_run = SqliteConnection.run

    def fail_after_insert(connection, statement, parameters=()):
        result = original_run(connection, statement, parameters)
        if statement.startswith("INSERT INTO training_due"):
            raise OSError("transaction interrupted")
        return result

    with monkeypatch.context() as scope:
        scope.setattr(SqliteConnection, "run", fail_after_insert)
        with pytest.raises(TrainingMaintenanceError) as rejected:
            index.save_due_state(due)
    assert isinstance(rejected.value.__cause__, OSError)
    if initialized:
        assert index.read_due_state(due.due_identity) is None
        assert index.load_state().due_states == (_records()[0],)
    else:
        with SqliteSession(path) as connection:
            assert connection.run("SELECT name FROM sqlite_master").fetchall() == []
        assert index.read_due_state(due.due_identity) is None
        empty = index.load_state()
        assert (empty.due_states, empty.reminder_claims, empty.reminders) == ((), (), ())

    index.save_due_state(due)

    assert TrainingMaintenanceIndex(path, sqlite_policy=SqlitePolicyFixture()).read_due_state(due.due_identity) == due


@pytest.mark.parametrize("table,column", (
    ("training_due", "matured_days"),
    ("training_reminder_claim", "claimed_at"),
    ("training_reminder", "attempted_at"),
))
def test_tampered_records_fail_hash_validation_without_rewriting(tmp_path: Path, table: str, column: str) -> None:
    path = tmp_path / "maintenance.sqlite3"
    index = TrainingMaintenanceIndex(path, sqlite_policy=SqlitePolicyFixture())
    _save_records(index)
    with SqliteSession(path) as connection, SqliteTransaction(connection):
        connection.run(f"UPDATE {table} SET {column}={column}+1")
    before = path.read_bytes()

    with pytest.raises(TrainingMaintenanceError, match="hash"):
        index.load_state()

    assert path.read_bytes() == before


@pytest.mark.parametrize("damage", ("unrelated", "missing_table", "corrupt"))
def test_incompatible_maintenance_is_rejected_without_upgrade_or_clear(tmp_path: Path, damage: str) -> None:
    path = tmp_path / "maintenance.sqlite3"
    index = TrainingMaintenanceIndex(path, sqlite_policy=SqlitePolicyFixture())
    if damage == "unrelated":
        with SqliteSession(path) as connection, SqliteTransaction(connection):
            connection.run("CREATE TABLE unrelated(value TEXT)")
    elif damage == "missing_table":
        _save_records(index)
        with SqliteSession(path) as connection, SqliteTransaction(connection):
            connection.run("DROP TABLE training_reminder")
    else:
        path.write_bytes(b"invalid database")
    before = _committed_files(tmp_path)

    with pytest.raises(TrainingMaintenanceError):
        index.load_state()
    with pytest.raises(TrainingMaintenanceError):
        index.save_due_state(_records()[0])

    assert _committed_files(tmp_path) == before


@pytest.mark.parametrize("kind", ("directory", "link", "broken_link"))
def test_unsafe_maintenance_path_is_rejected_without_creating_target(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "maintenance.sqlite3"
    target = tmp_path / "target.sqlite3"
    if kind == "directory":
        path.mkdir()
    else:
        if kind == "link":
            target.write_bytes(b"existing target")
        path.symlink_to(target)
    index = TrainingMaintenanceIndex(path, sqlite_policy=SqlitePolicyFixture())
    before = target.read_bytes() if target.exists() else None

    with pytest.raises(TrainingMaintenanceError, match="unsafe"):
        index.read_due_state("due-20260910")
    with pytest.raises(TrainingMaintenanceError, match="unsafe"):
        index.load_state()
    with pytest.raises(TrainingMaintenanceError, match="unsafe"):
        index.save_due_state(_records()[0])

    assert (target.read_bytes() if target.exists() else None) == before
    assert not path.with_suffix(".lock").exists()


def test_maintenance_writer_uses_its_own_nonblocking_lock(tmp_path: Path) -> None:
    path = tmp_path / "maintenance.sqlite3"
    index = TrainingMaintenanceIndex(path, sqlite_policy=SqlitePolicyFixture())
    _save_records(index)
    before = path.read_bytes()

    with ProcessLock(path.with_suffix(".lock")), pytest.raises(TrainingMaintenanceError) as rejected:
        index.save_due_state(replace(_records()[0], due_identity="locked"))

    assert isinstance(rejected.value.__cause__, maintenance_module.ProcessLockError)
    assert path.read_bytes() == before


def test_orphan_reminder_claim_is_rejected_by_foreign_key(tmp_path: Path) -> None:
    path = tmp_path / "maintenance.sqlite3"
    index = TrainingMaintenanceIndex(path, sqlite_policy=SqlitePolicyFixture())

    with pytest.raises(TrainingMaintenanceError) as rejected:
        index.claim_reminder(_records()[1])

    assert isinstance(rejected.value.__cause__, sqlite3.IntegrityError)


def test_maintenance_decodes_only_its_typed_records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "maintenance.sqlite3"
    index = TrainingMaintenanceIndex(path, sqlite_policy=SqlitePolicyFixture())
    _save_records(index)
    decoded: list[str] = []
    original_decode = maintenance_module._decode

    def track_decode(kind, row):
        decoded.append(kind)
        return original_decode(kind, row)

    monkeypatch.setattr(maintenance_module, "_decode", track_decode)

    index.load_state()

    assert decoded == ["due", "reminder_claim", "reminder"]
