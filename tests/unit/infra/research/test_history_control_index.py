from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import trader.download.infra.history_control_index as control_repository_module
import trader.training.infra.history.training_maintenance_index as maintenance_module
from trader.download.domain.history_control import (
    HistoryActiveSnapshot,
    HistoryCalendarIdentity,
    HistoryControlState,
    HistoryDiskRequirement,
    HistorySecurityIdentity,
    HistorySnapshotPartition,
    HistorySourceIdentity,
    HistorySyncCheckpoint,
    HistoryUniverseIdentity,
)
from trader.download.domain.history_reference import HistoryReferenceSnapshot, HistoryStEvidence
from trader.download.infra.history_control_index import (
    HistoryControlConflictError,
    HistoryControlError,
    HistoryControlIndex,
    HistoryControlRegressionError,
    HistoryMaintenanceAlreadyRunningError,
    HistoryMaintenanceLock,
    InspectHistoryDisk,
)
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction
from trader.training.domain.training_due import TrainingDueState
from trader.training.domain.training_reminder import TrainingReminderClaim, TrainingReminderState
from trader.training.infra.history.training_maintenance_index import TrainingMaintenanceIndex

NOW = datetime(2026, 9, 10, 20, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
REFERENCE = HistoryReferenceSnapshot(("600001",), (), (HistoryStEvidence("600001", NOW.date(), "clear"),))


def _values(sequence: int = 1, cutoff: date = date(2026, 9, 10)):
    source = HistorySourceIdentity("baostock", "baostock_daily", f"history_ref_{REFERENCE.content_hash}_fixture", NOW)
    calendar = HistoryCalendarIdentity((date(2026, 9, 9), cutoff), source.content_hash)
    universe = HistoryUniverseIdentity(
        (HistorySecurityIdentity("600001", "浦发银行", "main", date(1999, 11, 10), None),),
        source.content_hash,
    )
    checkpoint = HistorySyncCheckpoint("sync-20260910", 1, "completed", NOW, 2, 2, None)
    due = TrainingDueState("due-20260910", "initial_training_required", None, cutoff, 0, False, NOW)
    reminder_at = NOW.replace(year=cutoff.year, month=cutoff.month, day=cutoff.day)
    reminder = TrainingReminderState("due-20260910", cutoff, "notification_degraded", reminder_at, "notify_failed")
    reminder_claim = TrainingReminderClaim("due-20260910", cutoff, reminder_at)
    snapshot = HistoryActiveSnapshot(
        sequence,
        cutoff,
        cutoff,
        calendar.content_hash,
        universe.content_hash,
        source.content_hash,
        (HistorySnapshotPartition(f"prices_{cutoff:%Y}.sqlite3", "a" * 64, 2, calendar_month=9),),
    )
    return source, calendar, universe, checkpoint, due, reminder_claim, reminder, snapshot


def _save_all(repository: HistoryControlIndex, values) -> HistoryControlState:
    source, calendar, universe, checkpoint, due, reminder_claim, reminder, snapshot = values
    repository.save_source(source)
    repository.save_calendar(calendar)
    repository.save_universe(universe)
    repository.save_checkpoint(checkpoint)
    TrainingMaintenanceIndex(repository._path).save_due_state(due)
    TrainingMaintenanceIndex(repository._path).claim_reminder(reminder_claim)
    TrainingMaintenanceIndex(repository._path).save_reminder(reminder)
    repository.publish_snapshot(snapshot, reference=REFERENCE)
    return repository.load_state()


def test_control_repository_round_trips_typed_state_and_replays_same_content(tmp_path: Path) -> None:
    repository = HistoryControlIndex(tmp_path / "catalog.sqlite3")
    repository.initialize()
    values = _values()

    expected = _save_all(repository, values)
    replayed = _save_all(repository, values)

    assert replayed == expected
    assert replayed.active_snapshot == values[-1]
    assert TrainingMaintenanceIndex(repository._path).load_state().reminder_claims == (values[-3],)
    automation = TrainingMaintenanceIndex(repository._path).load_state()
    assert repository.load_published_state().snapshot == values[-1]
    assert automation.due_states == (values[4],)
    assert automation.reminder_claims == (values[5],)
    assert automation.reminders == (values[6],)
    assert repository.integrity().state == "healthy"


def test_failed_catalog_seed_leaves_no_tables_and_retry_creates_all_consumers(tmp_path, monkeypatch):
    path = tmp_path / "catalog.sqlite3"
    index = HistoryControlIndex(path)
    original = index._initialize_metadata

    def interrupt(connection):
        original(connection)
        raise RuntimeError("catalog seed interrupted")

    monkeypatch.setattr(index, "_initialize_metadata", interrupt)
    with pytest.raises(RuntimeError, match="seed interrupted"):
        index.initialize()
    with SqliteSession(path) as connection:
        assert connection.run("SELECT name FROM sqlite_master").fetchall() == []
    monkeypatch.setattr(index, "_initialize_metadata", original)
    index.initialize()
    with SqliteSession(path) as connection:
        tables = {row[0] for row in connection.run("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"metadata", "immutable_records", "active_snapshot", "bound_references", "download_gaps"} <= tables


def test_training_owner_reads_and_replays_baseline_catalog_bytes_without_rewriting(tmp_path):
    path = tmp_path / "catalog.sqlite3"
    HistoryControlIndex(path).initialize()
    # Captured from the original owner at 1bc1ce78; these are persisted protocol bytes.
    records = (
        (
            "due",
            "due-20260910",
            "a00e3fbeb447e112089fe4c8ee37f54b4380395b4f585141a9d19e4a632df7d5",
            '{"baseline_label_cutoff":null,"current_label_cutoff":"2026-09-10","due_identity":"due-20260910","input_revision":false,"matured_label_days_since_training":0,"observed_at":"2026-09-10T20:30:00+08:00","reason":"initial_training_required"}',
        ),
        (
            "reminder_claim",
            "due-20260910:2026-09-10",
            "73dfc48b74e90ecb37b7941178ab58fe4ed8833ad50ee40d6a8acd4bd1b03254",
            '{"claimed_at":"2026-09-10T20:30:00+08:00","due_identity":"due-20260910","reminder_date":"2026-09-10"}',
        ),
        (
            "reminder",
            "due-20260910:2026-09-10",
            "f0206043102594ffb0c5e3041d60ce6a5994ba45bdec962ebcfb241325ebc9d5",
            '{"attempted_at":"2026-09-10T20:30:00+08:00","due_identity":"due-20260910","error_code":"notify_failed","outcome":"notification_degraded","reminder_date":"2026-09-10"}',
        ),
    )
    with SqliteSession(path) as connection, SqliteTransaction(connection, immediate=True):
        for record in records:
            connection.run(
                "INSERT INTO immutable_records(kind,record_key,content_hash,payload_json) VALUES(?,?,?,?)", record
            )
    index = TrainingMaintenanceIndex(path)
    state = index.load_state()
    assert state.due_states == (_values()[4],)
    assert state.reminder_claims == (_values()[5],)
    assert state.reminders == (_values()[6],)
    index.restore_state(state)
    with SqliteSession(path) as connection:
        actual = connection.run(
            "SELECT kind,record_key,content_hash,payload_json FROM immutable_records ORDER BY kind,record_key"
        ).fetchall()
        assert [tuple(row) for row in actual] == sorted(records)


def test_corrupt_training_record_does_not_invalidate_published_download_facts(tmp_path):
    path = tmp_path / "catalog.sqlite3"
    history = HistoryControlIndex(path)
    history.initialize()
    original = _save_all(history, _values())
    with SqliteSession(path) as connection, SqliteTransaction(connection, immediate=True):
        connection.run("UPDATE immutable_records SET payload_json='{}' WHERE kind='due'")
    assert history.load_state() == original
    with pytest.raises(maintenance_module.TrainingMaintenanceError):
        TrainingMaintenanceIndex(path).load_state()


@pytest.mark.parametrize("catalog_state", ("published", "missing", "corrupt_training"))
def test_reminder_cli_reads_training_records_without_calculation_or_writes(
    tmp_path, monkeypatch, capsys, catalog_state
):
    import json

    from trader.entrypoints.cli import Main
    from trader.infra.settings import LoadRuntimeSettings
    from trader.infra.settings.data_paths import DataPaths

    config = Path(__file__).resolve().parents[4] / "config/runtime.json"
    settings = replace(LoadRuntimeSettings(config), data_paths=DataPaths(tmp_path / "data"))
    path = settings.data_paths.history / "catalog.sqlite3"
    if catalog_state != "missing":
        history = HistoryControlIndex(path)
        history.initialize()
        _save_all(history, _values())
        if catalog_state == "corrupt_training":
            with SqliteSession(path) as connection, SqliteTransaction(connection, immediate=True):
                connection.run("UPDATE immutable_records SET payload_json='{}' WHERE kind='due'")

    def forbid_mutation(*_args, **_kwargs):
        pytest.fail("read-only reminder status calculated or wrote maintenance state")

    monkeypatch.setattr("trader.entrypoints.cli.LoadRuntimeSettings", lambda _path: settings)
    monkeypatch.setattr("trader.bootstrap._utc_now", lambda: NOW)
    monkeypatch.setattr("trader.bootstrap.EvaluateAllHistoryTrainingDue", forbid_mutation)
    monkeypatch.setattr(HistoryControlIndex, "initialize", forbid_mutation)
    for method in ("save_due_state", "claim_reminder", "save_reminder"):
        monkeypatch.setattr(TrainingMaintenanceIndex, method, forbid_mutation)

    assert Main(["--config", str(config), "history-automation-status"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == "history_automation_status"
    assert payload["automatic_model_update"] is False
    if catalog_state == "published":
        assert payload["active_snapshot_hash"] == _values()[-1].content_hash
        assert payload["state"] == "ready"
        assert payload["training_due"] is True
        assert payload["reminder_state"] == "notification_degraded"
        assert payload["reminder_error_code"] == "notify_failed"
    else:
        assert payload["state"] == "data_incomplete"
        assert payload["training_due"] is False
        assert payload["reminder_state"] == "not_due"
    if catalog_state == "missing":
        assert not settings.data_paths.root.exists()


def test_missing_due_projection_is_unavailable_instead_of_data_incomplete():
    from trader.recommendation.domain.publication.models import Strategy
    from trader.training.domain.model_publication import HeadTrainingStatus
    from trader.training.infra.history.history_training_projection import ProjectHeadTrainingStatus

    payload = ProjectHeadTrainingStatus(HeadTrainingStatus("v2", Strategy.TOMORROW, None, None, "publication_busy"))
    assert payload["training_due"] is None
    assert payload["training_due_reason"] == "unavailable"
    assert payload["failure"] == "publication_busy"


def test_published_control_decodes_only_active_snapshot_and_three_parents(tmp_path, monkeypatch):
    repository = HistoryControlIndex(tmp_path / "catalog.sqlite3")
    repository.initialize()
    old = _values()
    _save_all(repository, old)
    current = _values(2, date(2026, 9, 11))
    repository.save_source(current[0])
    repository.save_calendar(current[1])
    repository.save_universe(current[2])
    repository.publish_snapshot(current[-1])
    decode = control_repository_module._decode_record
    kinds = []

    def observed(kind, payload):
        kinds.append(kind)
        return decode(kind, payload)

    monkeypatch.setattr(control_repository_module, "_decode_record", observed)
    published = repository.load_published_state()
    assert published.snapshot == current[-1]
    assert published.source == current[0]
    assert published.calendar == current[1]
    assert published.universe == current[2]
    assert kinds == ["snapshot", "source", "calendar", "universe"]


@pytest.mark.parametrize("kind", ("snapshot", "source", "calendar", "universe"))
def test_published_control_rejects_tampered_required_record(tmp_path, kind):
    path = tmp_path / "catalog.sqlite3"
    repository = HistoryControlIndex(path)
    repository.initialize()
    _save_all(repository, _values())
    with SqliteSession(path) as connection, SqliteTransaction(connection):
        connection.run("UPDATE immutable_records SET content_hash=? WHERE kind=?", ("f" * 64, kind))
    with pytest.raises(HistoryControlError, match="hash|pointer"):
        repository.load_published_state()


def test_published_control_without_active_snapshot_is_unavailable(tmp_path):
    repository = HistoryControlIndex(tmp_path / "catalog.sqlite3")
    repository.initialize()
    assert repository.load_published_state() is None


def test_control_repository_round_trips_training_contract_due(tmp_path: Path) -> None:
    repository = HistoryControlIndex(tmp_path / "catalog.sqlite3")
    repository.initialize()
    due = TrainingDueState(
        "due-contract",
        "training_contract_due",
        date(2026, 9, 9),
        date(2026, 9, 10),
        1,
        False,
        NOW,
    )

    TrainingMaintenanceIndex(repository._path).save_due_state(due)

    assert TrainingMaintenanceIndex(repository._path).load_state().due_states == (due,)


def test_automation_state_decodes_only_bounded_status_records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository = HistoryControlIndex(tmp_path / "catalog.sqlite3")
    repository.initialize()
    _save_all(repository, _values())
    decoded_kinds: list[str] = []
    original_decode = maintenance_module._decode_record

    def track_decode(kind, payload_json):
        decoded_kinds.append(kind)
        return original_decode(kind, payload_json)

    monkeypatch.setattr(maintenance_module, "_decode_record", track_decode)

    TrainingMaintenanceIndex(repository._path).load_state()

    assert set(decoded_kinds) == {"due", "reminder_claim", "reminder"}


def test_control_repository_rejects_same_identity_with_different_content(tmp_path: Path) -> None:
    repository = HistoryControlIndex(tmp_path / "catalog.sqlite3")
    repository.initialize()
    values = _values()
    repository.save_checkpoint(values[3])

    conflicting = replace(values[3], state="failed", completed_units=1, error_code="supplier_failed")
    with pytest.raises(HistoryControlConflictError, match="immutable"):
        repository.save_checkpoint(conflicting)


def test_snapshot_publication_recovers_after_interruption_without_pointer_regression(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    first_repository = HistoryControlIndex(path)
    first_repository.initialize()
    first = _values()
    _save_all(first_repository, first)
    second_values = _values(2, date(2026, 9, 11))
    second = second_values[-1]
    interrupted = HistoryControlIndex(path)
    interrupted.save_calendar(second_values[1])
    interrupted.save_universe(second_values[2])

    def interrupt(stage: str) -> None:
        if stage == "snapshot_committed":
            raise RuntimeError("simulated power loss")

    interrupted = HistoryControlIndex(path, fault_injector=interrupt)
    with pytest.raises(RuntimeError, match="power loss"):
        interrupted.publish_snapshot(second)
    assert interrupted.load_state().active_snapshot == first[-1]
    assert second not in interrupted.load_state().snapshots

    recovered = HistoryControlIndex(path)
    recovered.publish_snapshot(second)
    assert recovered.load_state().active_snapshot == second
    with pytest.raises(HistoryControlRegressionError, match="regress"):
        recovered.publish_snapshot(first[-1])


def test_snapshot_publication_rejects_unsealed_parent_identities(tmp_path: Path) -> None:
    repository = HistoryControlIndex(tmp_path / "catalog.sqlite3")
    repository.initialize()
    snapshot = _values()[-1]

    with pytest.raises(HistoryControlConflictError, match="parent identities"):
        repository.publish_snapshot(snapshot)


def test_reminder_claim_is_atomic_across_restart_and_dates(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    repository = HistoryControlIndex(path)
    repository.initialize()
    TrainingMaintenanceIndex(path).save_due_state(
        TrainingDueState("due-20260910", "initial_training_required", None, NOW.date(), 0, False, NOW)
    )
    first = TrainingReminderClaim("due-20260910", date(2026, 9, 10), NOW)

    assert TrainingMaintenanceIndex(repository._path).claim_reminder(first) is True
    restarted = TrainingMaintenanceIndex(path)
    assert restarted.claim_reminder(first) is False

    next_day_at = NOW.replace(day=11)
    next_day = TrainingReminderClaim("due-20260910", date(2026, 9, 11), next_day_at)
    assert restarted.claim_reminder(next_day) is True
    assert restarted.load_state().reminder_claims == (first, next_day)


def test_corrupt_control_database_rebuilds_from_verified_typed_state(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    repository = HistoryControlIndex(path)
    repository.initialize()
    expected = _save_all(repository, _values())
    maintenance = TrainingMaintenanceIndex(path).load_state()

    def restore(target: Path) -> None:
        TrainingMaintenanceIndex(target).restore_state(maintenance)

    for candidate in (Path(f"{path}-wal"), Path(f"{path}-shm")):
        candidate.unlink(missing_ok=True)
    corrupt_bytes = b"corrupt-control-database"
    path.write_bytes(corrupt_bytes)

    assert repository.integrity().state == "corrupt"

    def interrupt(stage: str) -> None:
        if stage == "rebuild_ready":
            raise RuntimeError("simulated rebuild interruption")

    with pytest.raises(RuntimeError, match="rebuild interruption"):
        HistoryControlIndex.rebuild(
            path, expected, references=(REFERENCE,), restore_related_records=restore, fault_injector=interrupt
        )
    assert path.read_bytes() == corrupt_bytes

    HistoryControlIndex.rebuild(path, expected, references=(REFERENCE,), restore_related_records=restore)

    rebuilt = HistoryControlIndex(path)
    assert rebuilt.integrity().state == "healthy"
    assert rebuilt.load_state() == expected
    assert TrainingMaintenanceIndex(path).load_state() == maintenance


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
    sufficient = InspectHistoryDisk(
        tmp_path / "missing" / "archive",
        requirement,
        disk_usage=lambda _path: SimpleNamespace(free=160),
    )
    insufficient = InspectHistoryDisk(
        tmp_path,
        requirement,
        disk_usage=lambda _path: SimpleNamespace(free=159),
    )
    assert sufficient.sufficient is True
    assert sufficient.required_free_bytes == 160
    assert insufficient.sufficient is False
