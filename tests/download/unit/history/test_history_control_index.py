from __future__ import annotations

from tests.helpers.shared.configuration import SqlitePolicyFixture

from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import trader.download.infra.history_control_index as control_index_module
import trader.training.infra.history.training_maintenance_index as maintenance_module
from tests.helpers.shared.reference_fixtures import PublishHistoryReferenceFixture
from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.download.domain.history_control import (
    HistoryCalendarIdentity,
    HistoryControlState,
    HistoryDiskRequirement,
    HistorySecurityIdentity,
    HistorySnapshot,
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
    HistoryRebuildInput,
    InspectHistoryDisk,
)
from trader.download.infra.published_history_snapshot import SQLitePublishedHistorySnapshot
from trader.infra.sqlite_connection import SqliteConnection, SqliteSession, SqliteTransaction
from trader.infra.sqlite_publication import SqlitePublicationError, VerifySqliteFile
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
    snapshot = HistorySnapshot(
        sequence,
        cutoff,
        cutoff,
        calendar.content_hash,
        universe.content_hash,
        source.content_hash,
        (HistorySnapshotPartition(f"prices_{cutoff:%Y}.sqlite3", "a" * 64, 2, calendar_month=9),),
    )
    return source, calendar, universe, checkpoint, due, reminder_claim, reminder, snapshot


def _publication(index, calendar, universe):
    reference_path = index._path.parent / "company_st.sqlite3"
    publication_id = PublishHistoryReferenceFixture(
        reference_path,
        REFERENCE,
        securities=tuple(
            BaoStockSecurity(item.code, item.name, item.board, item.listed_on, item.delisted_on, "fixture")
            for item in universe.securities
        ),
        open_dates=calendar.open_dates if calendar.open_dates[-1] >= NOW.date() else (*calendar.open_dates, NOW.date()),
    )
    index.save_reference(REFERENCE, publication_id)
    return publication_id


def _maintenance_path(index: HistoryControlIndex) -> Path:
    return index._path.parent / "training" / "maintenance.sqlite3"


def _save_all(index: HistoryControlIndex, values, *, maintenance_path: Path | None = None) -> HistoryControlState:
    source, calendar, universe, checkpoint, due, reminder_claim, reminder, snapshot = values
    publication_id = _publication(index, calendar, universe)
    index.save_source(source)
    index.save_calendar(calendar, publication_id)
    index.save_universe(universe, publication_id)
    index.save_checkpoint(checkpoint)
    maintenance = TrainingMaintenanceIndex(
        _maintenance_path(index) if maintenance_path is None else maintenance_path,
        sqlite_policy=SqlitePolicyFixture(),
    )
    maintenance.save_due_state(due)
    maintenance.claim_reminder(reminder_claim)
    maintenance.save_reminder(reminder)
    index.publish_snapshot(snapshot, reference=REFERENCE)
    return index.load_state()


def test_input_batch_rolls_back_reference_and_parents_when_universe_disagrees(tmp_path: Path) -> None:
    path = tmp_path / "company_st.sqlite3"
    index = HistoryControlIndex(tmp_path / "catalog.sqlite3", reference_path=path, sqlite_policy=SqlitePolicyFixture())
    index.initialize()
    source, calendar, universe, *_ = _values()
    publication_id = PublishHistoryReferenceFixture(
        path,
        REFERENCE,
        securities=tuple(
            BaoStockSecurity(item.code, item.name, item.board, item.listed_on, item.delisted_on, "fixture")
            for item in universe.securities
        ),
        open_dates=calendar.open_dates,
    )
    before = index.load_state()
    invalid = replace(universe, securities=(replace(universe.securities[0], name="不匹配的名称"),))
    with pytest.raises(ValueError, match="history_universe_binding_invalid"):
        index.save_inputs((source, calendar, invalid), REFERENCE, publication_id)
    assert index.load_state() == before
    with SqliteSession(tmp_path / "catalog.sqlite3") as connection:
        assert connection.run("SELECT count(*) FROM reference_binding").require_one()[0] == 0
    index.save_inputs((source, calendar, universe), REFERENCE, publication_id)
    after = index.load_state()
    assert after.sources == (source,)
    assert after.calendars == (calendar,)
    assert after.universes == (universe,)


def test_same_day_reference_change_keeps_each_calendar_and_universe_bound(tmp_path: Path) -> None:
    index = HistoryControlIndex(tmp_path / "catalog.sqlite3", reference_path=tmp_path / "company_st.sqlite3", sqlite_policy=SqlitePolicyFixture())
    index.initialize()
    first = _save_all(index, _values())
    source, calendar, universe, *_ = _values()
    renamed = replace(universe, securities=(replace(universe.securities[0], name="新证券名称"),))
    extended = replace(calendar, open_dates=(date(2026, 9, 8), *calendar.open_dates))
    publication = _publication(index, extended, renamed)
    index.save_calendar(extended, publication)
    index.save_universe(renamed, publication)
    snapshot = replace(
        first.published_snapshot, sequence=2, calendar_hash=extended.content_hash, universe_hash=renamed.content_hash
    )
    index.publish_snapshot(snapshot, reference=REFERENCE)
    previous = index.load_published_state(sequence=1)
    current = index.load_published_state()
    assert previous.calendar == calendar and previous.universe == universe
    assert current.calendar == extended and current.universe == renamed
    assert current.source == source


def test_control_index_round_trips_typed_state_and_replays_same_content(tmp_path: Path) -> None:
    index = HistoryControlIndex(
        tmp_path / "catalog.sqlite3", reference_path=(tmp_path / "catalog.sqlite3").parent / "company_st.sqlite3", sqlite_policy=SqlitePolicyFixture()
    )
    index.initialize()
    values = _values()

    expected = _save_all(index, values)
    replayed = _save_all(index, values)

    assert replayed == expected
    assert replayed.published_snapshot == values[-1]
    assert TrainingMaintenanceIndex(_maintenance_path(index), sqlite_policy=SqlitePolicyFixture()).load_state().reminder_claims == (values[-3],)
    automation = TrainingMaintenanceIndex(_maintenance_path(index), sqlite_policy=SqlitePolicyFixture()).load_state()
    assert index.load_published_state().snapshot == values[-1]
    assert automation.due_states == (values[4],)
    assert automation.reminder_claims == (values[5],)
    assert automation.reminders == (values[6],)
    assert index.integrity().state == "healthy"


def test_failed_catalog_seed_leaves_no_tables_and_retry_creates_all_consumers(tmp_path, monkeypatch):
    path = tmp_path / "catalog.sqlite3"
    index = HistoryControlIndex(path, reference_path=(path).parent / "company_st.sqlite3", sqlite_policy=SqlitePolicyFixture())
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
    assert {"schema_contract", "source_version", "published_snapshot", "reference_binding", "download_gaps"} <= tables


@pytest.mark.parametrize("kind", ("due", "claim", "reminder"))
def test_training_write_leaves_published_history_and_receipt_unchanged(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "history" / "catalog.sqlite3"
    control = HistoryControlIndex(path, reference_path=path.parent / "company_st.sqlite3", sqlite_policy=SqlitePolicyFixture())
    control.initialize()
    values = _values()
    expected = _save_all(control, values)
    index = TrainingMaintenanceIndex(_maintenance_path(control), sqlite_policy=SqlitePolicyFixture())
    due = replace(values[4], due_identity="due-after-publication")
    if kind != "due":
        index.save_due_state(due)
    reader = SQLitePublishedHistorySnapshot(path.parent, sqlite_policy=SqlitePolicyFixture())
    before = reader.manifest()
    files = _history_files(path.parent)

    if kind == "due":
        index.save_due_state(due)
    elif kind == "claim":
        assert index.claim_reminder(TrainingReminderClaim(due.due_identity, NOW.date(), NOW))
    else:
        index.save_reminder(TrainingReminderState(due.due_identity, NOW.date(), "sent", NOW, None))

    VerifySqliteFile(path, sqlite_policy=SqlitePolicyFixture())
    assert _history_files(path.parent) == files
    assert control.load_state() == expected
    assert reader.manifest() == before
    assert SQLitePublishedHistorySnapshot(path.parent, sqlite_policy=SqlitePolicyFixture()).manifest() == before


def test_training_replay_reads_and_conflict_do_not_reseal_catalog(tmp_path: Path) -> None:
    path = tmp_path / "history" / "catalog.sqlite3"
    control = HistoryControlIndex(path, reference_path=path.parent / "company_st.sqlite3", sqlite_policy=SqlitePolicyFixture())
    control.initialize()
    values = _values()
    _save_all(control, values)
    index = TrainingMaintenanceIndex(_maintenance_path(control), sqlite_policy=SqlitePolicyFixture())
    receipt = Path(f"{path}.sha256")
    before = path.read_bytes(), receipt.read_bytes(), receipt.stat()

    index.save_due_state(values[4])
    assert not index.claim_reminder(values[5])
    index.save_reminder(values[6])
    assert index.read_due_state(values[4].due_identity) == values[4]
    index.load_state()
    with pytest.raises(maintenance_module.TrainingMaintenanceConflictError):
        index.save_due_state(replace(values[4], input_revision=True))

    assert (path.read_bytes(), receipt.read_bytes(), receipt.stat()) == before
    VerifySqliteFile(path, sqlite_policy=SqlitePolicyFixture())


def _history_files(root: Path) -> dict[str, bytes]:
    # SQLite read-only handles may build a shared index and an empty WAL. Neither
    # contains committed database changes; nonempty WAL bytes remain protected.
    return {
        file.name: file.read_bytes()
        for file in root.iterdir()
        if file.is_file() and not file.name.endswith("-shm")
        and not (file.name.endswith("-wal") and file.stat().st_size == 0)
    }


@pytest.mark.parametrize("damage", ("valid", "checksum", "missing_receipt"))
def test_training_owner_rejects_a_history_catalog_without_changing_committed_files(tmp_path: Path, damage: str) -> None:
    path = tmp_path / "history" / "catalog.sqlite3"
    control = HistoryControlIndex(path, reference_path=path.parent / "company_st.sqlite3", sqlite_policy=SqlitePolicyFixture())
    control.initialize()
    values = _values()
    _save_all(control, values)
    receipt = Path(f"{path}.sha256")
    if damage == "checksum":
        with SqliteSession(path) as connection, SqliteTransaction(connection):
            connection.run("UPDATE source_version SET source='tampered'")
    elif damage == "missing_receipt":
        receipt.unlink()
    before = _history_files(path.parent)

    with pytest.raises(maintenance_module.TrainingMaintenanceError):
        TrainingMaintenanceIndex(path, sqlite_policy=SqlitePolicyFixture()).save_due_state(
            replace(values[4], due_identity="due-after-damage")
        )

    assert _history_files(path.parent) == before
    assert not path.with_suffix(".lock").exists()
    if damage == "valid":
        VerifySqliteFile(path, sqlite_policy=SqlitePolicyFixture())
    else:
        with pytest.raises(SqlitePublicationError):
            VerifySqliteFile(path, sqlite_policy=SqlitePolicyFixture())


def test_training_owner_rejects_a_catalog_with_committed_wal_without_checkpointing(tmp_path: Path) -> None:
    path = tmp_path / "history" / "catalog.sqlite3"
    control = HistoryControlIndex(path, reference_path=path.parent / "company_st.sqlite3", sqlite_policy=SqlitePolicyFixture())
    control.initialize()
    _save_all(control, _values())
    with SqliteSession(path) as writer:
        with SqliteTransaction(writer):
            writer.run("UPDATE source_version SET observed_at=observed_at+1")
        assert Path(f"{path}-wal").stat().st_size > 32
        before = _history_files(path.parent)

        with pytest.raises(maintenance_module.TrainingMaintenanceError):
            TrainingMaintenanceIndex(path, sqlite_policy=SqlitePolicyFixture()).save_due_state(_values()[4])

        assert _history_files(path.parent) == before
        assert writer.run("SELECT name FROM sqlite_master WHERE name LIKE 'training_%'").fetchall() == []
        assert not path.with_suffix(".lock").exists()


def test_training_write_is_independent_of_history_maintenance_lock(tmp_path: Path) -> None:
    path = tmp_path / "history" / "catalog.sqlite3"
    control = HistoryControlIndex(path, sqlite_policy=SqlitePolicyFixture())
    control.initialize()
    before = path.read_bytes()
    with HistoryMaintenanceLock(path.parent.with_name(".history.lock")):
        TrainingMaintenanceIndex(_maintenance_path(control), sqlite_policy=SqlitePolicyFixture()).save_due_state(_values()[4])
    assert path.read_bytes() == before
    assert not Path(f"{path}.sha256").exists()


def test_training_transaction_failure_keeps_published_history_readable_and_retries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "history" / "catalog.sqlite3"
    control = HistoryControlIndex(path, reference_path=path.parent / "company_st.sqlite3", sqlite_policy=SqlitePolicyFixture())
    control.initialize()
    values = _values()
    _save_all(control, values)
    due = replace(values[4], due_identity="due-transaction-failed")
    reader = SQLitePublishedHistorySnapshot(path.parent, sqlite_policy=SqlitePolicyFixture())
    original_manifest = reader.manifest()
    before = _history_files(path.parent)
    original_run = SqliteConnection.run

    def fail_after_insert(connection, statement, parameters=()):
        result = original_run(connection, statement, parameters)
        if statement.startswith("INSERT INTO training_due"):
            raise OSError("transaction interrupted")
        return result

    index = TrainingMaintenanceIndex(_maintenance_path(control), sqlite_policy=SqlitePolicyFixture())
    with monkeypatch.context() as scope:
        scope.setattr(SqliteConnection, "run", fail_after_insert)
        with pytest.raises(maintenance_module.TrainingMaintenanceError) as rejected:
            index.save_due_state(due)
    assert isinstance(rejected.value.__cause__, OSError)
    assert index.read_due_state(due.due_identity) is None
    assert reader.manifest() == original_manifest
    assert SQLitePublishedHistorySnapshot(path.parent, sqlite_policy=SqlitePolicyFixture()).manifest() == original_manifest
    assert _history_files(path.parent) == before
    index.save_due_state(due)
    assert index.read_due_state(due.due_identity) == due
    assert _history_files(path.parent) == before
    VerifySqliteFile(path, sqlite_policy=SqlitePolicyFixture())


def test_corrupt_training_record_does_not_invalidate_published_download_facts(tmp_path):
    path = tmp_path / "catalog.sqlite3"
    history = HistoryControlIndex(path, reference_path=(path).parent / "company_st.sqlite3", sqlite_policy=SqlitePolicyFixture())
    history.initialize()
    original = _save_all(history, _values())
    maintenance_path = _maintenance_path(history)
    files = _history_files(path.parent)
    reader = SQLitePublishedHistorySnapshot(path.parent, sqlite_policy=SqlitePolicyFixture())
    manifest = reader.manifest()
    with SqliteSession(maintenance_path) as connection, SqliteTransaction(connection, immediate=True):
        connection.run("UPDATE training_due SET matured_days=matured_days+1")
    assert history.load_state() == original
    with pytest.raises(maintenance_module.TrainingMaintenanceError):
        TrainingMaintenanceIndex(maintenance_path, sqlite_policy=SqlitePolicyFixture()).load_state()
    assert reader.manifest() == manifest
    assert _history_files(path.parent) == files


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
        history = HistoryControlIndex(path, reference_path=(path).parent / "company_st.sqlite3", sqlite_policy=SqlitePolicyFixture())
        history.initialize()
        _save_all(history, _values(), maintenance_path=settings.data_paths.training_maintenance)
        if catalog_state == "corrupt_training":
            with SqliteSession(settings.data_paths.training_maintenance) as connection, SqliteTransaction(connection, immediate=True):
                connection.run("UPDATE training_due SET matured_days=matured_days+1")

    def forbid_mutation(*_args, **_kwargs):
        pytest.fail("read-only reminder status calculated or wrote maintenance state")

    monkeypatch.setattr("trader.entrypoints.cli.LoadRuntimeSettings", lambda _path: settings)
    monkeypatch.setattr("trader.bootstrap._utc_now", lambda: NOW)
    monkeypatch.setattr(
        "trader.training.infra.history.history_training_due.EvaluateHistoryTrainingDue", forbid_mutation
    )
    monkeypatch.setattr(HistoryControlIndex, "initialize", forbid_mutation)
    for method in ("save_due_state", "claim_reminder", "save_reminder"):
        monkeypatch.setattr(TrainingMaintenanceIndex, method, forbid_mutation)

    assert Main(["--config", str(config), "history-automation-status"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == "history_automation_status"
    assert payload["automatic_model_update"] is False
    if catalog_state == "published":
        assert payload["snapshot_hash"] == _values()[-1].content_hash
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


def test_published_control_decodes_only_published_snapshot_and_three_parents(tmp_path, monkeypatch):
    index = HistoryControlIndex(
        tmp_path / "catalog.sqlite3", reference_path=(tmp_path / "catalog.sqlite3").parent / "company_st.sqlite3", sqlite_policy=SqlitePolicyFixture()
    )
    index.initialize()
    old = _values()
    _save_all(index, old)
    current = _values(2, date(2026, 9, 11))
    index.save_source(current[0])
    index.save_calendar(current[1], _publication(index, current[1], current[2]))
    index.save_universe(current[2], _publication(index, current[1], current[2]))
    index.publish_snapshot(current[-1])
    decode = control_index_module.ReadControlRecord
    kinds = []

    def observed(connection, kind, key, index, references=None):
        kinds.append(kind)
        return decode(connection, kind, key, index, references)

    monkeypatch.setattr(control_index_module, "ReadControlRecord", observed)
    published = index.load_published_state()
    assert published.snapshot == current[-1]
    assert published.source == current[0]
    assert published.calendar == current[1]
    assert published.universe == current[2]
    assert kinds == ["snapshot", "source", "calendar", "universe"]


@pytest.mark.parametrize("kind", ("snapshot", "source", "calendar", "universe"))
def test_published_control_rejects_tampered_required_record(tmp_path, kind):
    path = tmp_path / "catalog.sqlite3"
    index = HistoryControlIndex(path, reference_path=(path).parent / "company_st.sqlite3", sqlite_policy=SqlitePolicyFixture())
    index.initialize()
    _save_all(index, _values())
    table, column = {
        "source": ("source_version", "source"),
        "calendar": ("calendar_binding", "session_count"),
        "universe": ("universe_version", "security_count"),
        "checkpoint": ("sync_checkpoint", "content_hash"),
        "snapshot": ("history_snapshot", "content_hash"),
    }[kind]
    with SqliteSession(path) as connection, SqliteTransaction(connection):
        connection.run(f"UPDATE {table} SET {column}=?", (bytes(32) if column == "content_hash" else "corrupt",))
    with pytest.raises(HistoryControlError, match="read failed|pointer"):
        index.load_published_state()


def test_published_control_without_published_snapshot_is_unavailable(tmp_path):
    index = HistoryControlIndex(
        tmp_path / "catalog.sqlite3", reference_path=(tmp_path / "catalog.sqlite3").parent / "company_st.sqlite3", sqlite_policy=SqlitePolicyFixture()
    )
    index.initialize()
    assert index.load_published_state() is None


def test_control_index_rejects_same_identity_with_different_content(tmp_path: Path) -> None:
    index = HistoryControlIndex(
        tmp_path / "catalog.sqlite3", reference_path=(tmp_path / "catalog.sqlite3").parent / "company_st.sqlite3", sqlite_policy=SqlitePolicyFixture()
    )
    index.initialize()
    values = _values()
    index.save_checkpoint(values[3])

    conflicting = replace(values[3], state="failed", completed_units=1, error_code="supplier_failed")
    with pytest.raises(HistoryControlConflictError, match="immutable"):
        index.save_checkpoint(conflicting)


def test_snapshot_publication_recovers_after_interruption_without_pointer_regression(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    first_index = HistoryControlIndex(path, reference_path=(path).parent / "company_st.sqlite3", sqlite_policy=SqlitePolicyFixture())
    first_index.initialize()
    first = _values()
    _save_all(first_index, first)
    second_values = _values(2, date(2026, 9, 11))
    second = second_values[-1]
    interrupted = HistoryControlIndex(path, reference_path=(path).parent / "company_st.sqlite3", sqlite_policy=SqlitePolicyFixture())
    interrupted.save_calendar(second_values[1], _publication(interrupted, second_values[1], second_values[2]))
    interrupted.save_universe(second_values[2], _publication(interrupted, second_values[1], second_values[2]))

    def interrupt(stage: str) -> None:
        if stage == "snapshot_committed":
            raise RuntimeError("simulated power loss")

    interrupted = HistoryControlIndex(
        path, reference_path=(path).parent / "company_st.sqlite3", fault_injector=interrupt, sqlite_policy=SqlitePolicyFixture()
    )
    with pytest.raises(RuntimeError, match="power loss"):
        interrupted.publish_snapshot(second)
    assert interrupted.load_state().published_snapshot == first[-1]
    assert second not in interrupted.load_state().snapshots

    recovered = HistoryControlIndex(path, reference_path=(path).parent / "company_st.sqlite3", sqlite_policy=SqlitePolicyFixture())
    recovered.publish_snapshot(second)
    assert recovered.load_state().published_snapshot == second
    with pytest.raises(HistoryControlRegressionError, match="regress"):
        recovered.publish_snapshot(first[-1])


def test_snapshot_publication_rejects_unsealed_parent_identities(tmp_path: Path) -> None:
    index = HistoryControlIndex(
        tmp_path / "catalog.sqlite3", reference_path=(tmp_path / "catalog.sqlite3").parent / "company_st.sqlite3", sqlite_policy=SqlitePolicyFixture()
    )
    index.initialize()
    snapshot = _values()[-1]

    with pytest.raises(HistoryControlConflictError, match="parent identities"):
        index.publish_snapshot(snapshot)


def test_corrupt_control_database_rebuild_keeps_independent_maintenance_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    index = HistoryControlIndex(path, reference_path=(path).parent / "company_st.sqlite3", sqlite_policy=SqlitePolicyFixture())
    index.initialize()
    expected = _save_all(index, _values())
    publication_id = _publication(index, _values()[1], _values()[2])
    reference_ids = tuple((item.content_hash, publication_id) for item in (*expected.calendars, *expected.universes))
    inputs = HistoryRebuildInput(
        expected, path.parent / "company_st.sqlite3", ((REFERENCE, publication_id),), reference_ids
    )
    maintenance_path = _maintenance_path(index)
    maintenance = TrainingMaintenanceIndex(maintenance_path, sqlite_policy=SqlitePolicyFixture()).load_state()
    maintenance_bytes = maintenance_path.read_bytes()

    for candidate in (Path(f"{path}-wal"), Path(f"{path}-shm")):
        candidate.unlink(missing_ok=True)
    corrupt_bytes = b"corrupt-control-database"
    path.write_bytes(corrupt_bytes)

    assert index.integrity().state == "corrupt"

    def interrupt(stage: str) -> None:
        if stage == "rebuild_ready":
            raise RuntimeError("simulated rebuild interruption")

    with pytest.raises(RuntimeError, match="rebuild interruption"):
        HistoryControlIndex.rebuild(
            path,
            inputs,
            fault_injector=interrupt, sqlite_policy=SqlitePolicyFixture(),
        )
    assert path.read_bytes() == corrupt_bytes

    HistoryControlIndex.rebuild(
        path,
        inputs,
        sqlite_policy=SqlitePolicyFixture(),
    )

    rebuilt = HistoryControlIndex(path, reference_path=(path).parent / "company_st.sqlite3", sqlite_policy=SqlitePolicyFixture())
    assert rebuilt.integrity().state == "healthy"
    assert rebuilt.load_state() == expected
    assert maintenance_path.read_bytes() == maintenance_bytes
    assert TrainingMaintenanceIndex(maintenance_path, sqlite_policy=SqlitePolicyFixture()).load_state() == maintenance
    with SqliteSession(path) as connection:
        assert connection.run("SELECT name FROM sqlite_master WHERE name LIKE 'training_%'").fetchall() == []


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


pytestmark = [pytest.mark.slow_history, pytest.mark.slow_migration]
