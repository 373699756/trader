from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import trader.download.infra.history_control_index as control_index_module
import trader.training.infra.history.training_maintenance_index as maintenance_module
from tests.reference_fixtures import PublishHistoryReferenceFixture
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


def _save_all(index: HistoryControlIndex, values) -> HistoryControlState:
    source, calendar, universe, checkpoint, due, reminder_claim, reminder, snapshot = values
    publication_id = _publication(index, calendar, universe)
    index.save_source(source)
    index.save_calendar(calendar, publication_id)
    index.save_universe(universe, publication_id)
    index.save_checkpoint(checkpoint)
    TrainingMaintenanceIndex(index._path).save_due_state(due)
    TrainingMaintenanceIndex(index._path).claim_reminder(reminder_claim)
    TrainingMaintenanceIndex(index._path).save_reminder(reminder)
    index.publish_snapshot(snapshot, reference=REFERENCE)
    return index.load_state()


def test_input_batch_rolls_back_reference_and_parents_when_universe_disagrees(tmp_path: Path) -> None:
    path = tmp_path / "company_st.sqlite3"
    index = HistoryControlIndex(tmp_path / "catalog.sqlite3", reference_path=path)
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
    index = HistoryControlIndex(tmp_path / "catalog.sqlite3", reference_path=tmp_path / "company_st.sqlite3")
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
        tmp_path / "catalog.sqlite3", reference_path=(tmp_path / "catalog.sqlite3").parent / "company_st.sqlite3"
    )
    index.initialize()
    values = _values()

    expected = _save_all(index, values)
    replayed = _save_all(index, values)

    assert replayed == expected
    assert replayed.published_snapshot == values[-1]
    assert TrainingMaintenanceIndex(index._path).load_state().reminder_claims == (values[-3],)
    automation = TrainingMaintenanceIndex(index._path).load_state()
    assert index.load_published_state().snapshot == values[-1]
    assert automation.due_states == (values[4],)
    assert automation.reminder_claims == (values[5],)
    assert automation.reminders == (values[6],)
    assert index.integrity().state == "healthy"


def test_failed_catalog_seed_leaves_no_tables_and_retry_creates_all_consumers(tmp_path, monkeypatch):
    path = tmp_path / "catalog.sqlite3"
    index = HistoryControlIndex(path, reference_path=(path).parent / "company_st.sqlite3")
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


def test_training_owner_retains_logical_hashes_and_replays_without_rewriting(tmp_path):
    path = tmp_path / "catalog.sqlite3"
    HistoryControlIndex(path).initialize()
    values = _values()
    index = TrainingMaintenanceIndex(path)
    index.save_due_state(values[4])
    index.claim_reminder(values[5])
    index.save_reminder(values[6])
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
    index.copy_state(state)
    assert path.read_bytes() == before
    assert index.load_state() == state


def test_corrupt_training_record_does_not_invalidate_published_download_facts(tmp_path):
    path = tmp_path / "catalog.sqlite3"
    history = HistoryControlIndex(path, reference_path=(path).parent / "company_st.sqlite3")
    history.initialize()
    original = _save_all(history, _values())
    with SqliteSession(path) as connection, SqliteTransaction(connection, immediate=True):
        connection.run("UPDATE training_due SET matured_days=matured_days+1")
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
        history = HistoryControlIndex(path, reference_path=(path).parent / "company_st.sqlite3")
        history.initialize()
        _save_all(history, _values())
        if catalog_state == "corrupt_training":
            with SqliteSession(path) as connection, SqliteTransaction(connection, immediate=True):
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
        tmp_path / "catalog.sqlite3", reference_path=(tmp_path / "catalog.sqlite3").parent / "company_st.sqlite3"
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
    index = HistoryControlIndex(path, reference_path=(path).parent / "company_st.sqlite3")
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
        tmp_path / "catalog.sqlite3", reference_path=(tmp_path / "catalog.sqlite3").parent / "company_st.sqlite3"
    )
    index.initialize()
    assert index.load_published_state() is None


def test_control_index_round_trips_training_contract_due(tmp_path: Path) -> None:
    index = HistoryControlIndex(
        tmp_path / "catalog.sqlite3", reference_path=(tmp_path / "catalog.sqlite3").parent / "company_st.sqlite3"
    )
    index.initialize()
    due = TrainingDueState(
        "due-contract",
        "training_contract_due",
        date(2026, 9, 9),
        date(2026, 9, 10),
        1,
        False,
        NOW,
    )

    TrainingMaintenanceIndex(index._path).save_due_state(due)

    assert TrainingMaintenanceIndex(index._path).load_state().due_states == (due,)


def test_automation_state_decodes_only_bounded_status_records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    index = HistoryControlIndex(
        tmp_path / "catalog.sqlite3", reference_path=(tmp_path / "catalog.sqlite3").parent / "company_st.sqlite3"
    )
    index.initialize()
    _save_all(index, _values())
    decoded_kinds: list[str] = []
    original_decode = maintenance_module._decode

    def track_decode(kind, payload_json):
        decoded_kinds.append(kind)
        return original_decode(kind, payload_json)

    monkeypatch.setattr(maintenance_module, "_decode", track_decode)

    TrainingMaintenanceIndex(index._path).load_state()

    assert set(decoded_kinds) == {"due", "reminder_claim", "reminder"}


def test_control_index_rejects_same_identity_with_different_content(tmp_path: Path) -> None:
    index = HistoryControlIndex(
        tmp_path / "catalog.sqlite3", reference_path=(tmp_path / "catalog.sqlite3").parent / "company_st.sqlite3"
    )
    index.initialize()
    values = _values()
    index.save_checkpoint(values[3])

    conflicting = replace(values[3], state="failed", completed_units=1, error_code="supplier_failed")
    with pytest.raises(HistoryControlConflictError, match="immutable"):
        index.save_checkpoint(conflicting)


def test_snapshot_publication_recovers_after_interruption_without_pointer_regression(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    first_index = HistoryControlIndex(path, reference_path=(path).parent / "company_st.sqlite3")
    first_index.initialize()
    first = _values()
    _save_all(first_index, first)
    second_values = _values(2, date(2026, 9, 11))
    second = second_values[-1]
    interrupted = HistoryControlIndex(path, reference_path=(path).parent / "company_st.sqlite3")
    interrupted.save_calendar(second_values[1], _publication(interrupted, second_values[1], second_values[2]))
    interrupted.save_universe(second_values[2], _publication(interrupted, second_values[1], second_values[2]))

    def interrupt(stage: str) -> None:
        if stage == "snapshot_committed":
            raise RuntimeError("simulated power loss")

    interrupted = HistoryControlIndex(
        path, reference_path=(path).parent / "company_st.sqlite3", fault_injector=interrupt
    )
    with pytest.raises(RuntimeError, match="power loss"):
        interrupted.publish_snapshot(second)
    assert interrupted.load_state().published_snapshot == first[-1]
    assert second not in interrupted.load_state().snapshots

    recovered = HistoryControlIndex(path, reference_path=(path).parent / "company_st.sqlite3")
    recovered.publish_snapshot(second)
    assert recovered.load_state().published_snapshot == second
    with pytest.raises(HistoryControlRegressionError, match="regress"):
        recovered.publish_snapshot(first[-1])


def test_snapshot_publication_rejects_unsealed_parent_identities(tmp_path: Path) -> None:
    index = HistoryControlIndex(
        tmp_path / "catalog.sqlite3", reference_path=(tmp_path / "catalog.sqlite3").parent / "company_st.sqlite3"
    )
    index.initialize()
    snapshot = _values()[-1]

    with pytest.raises(HistoryControlConflictError, match="parent identities"):
        index.publish_snapshot(snapshot)


def test_reminder_claim_is_atomic_across_restart_and_dates(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    index = HistoryControlIndex(path, reference_path=(path).parent / "company_st.sqlite3")
    index.initialize()
    TrainingMaintenanceIndex(path).save_due_state(
        TrainingDueState("due-20260910", "initial_training_required", None, NOW.date(), 0, False, NOW)
    )
    first = TrainingReminderClaim("due-20260910", date(2026, 9, 10), NOW)

    assert TrainingMaintenanceIndex(index._path).claim_reminder(first) is True
    restarted = TrainingMaintenanceIndex(path)
    assert restarted.claim_reminder(first) is False

    next_day_at = NOW.replace(day=11)
    next_day = TrainingReminderClaim("due-20260910", date(2026, 9, 11), next_day_at)
    assert restarted.claim_reminder(next_day) is True
    assert restarted.load_state().reminder_claims == (first, next_day)


def test_corrupt_control_database_rebuilds_from_verified_typed_state(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    index = HistoryControlIndex(path, reference_path=(path).parent / "company_st.sqlite3")
    index.initialize()
    expected = _save_all(index, _values())
    publication_id = _publication(index, _values()[1], _values()[2])
    reference_ids = tuple((item.content_hash, publication_id) for item in (*expected.calendars, *expected.universes))
    inputs = HistoryRebuildInput(
        expected, path.parent / "company_st.sqlite3", ((REFERENCE, publication_id),), reference_ids
    )
    maintenance = TrainingMaintenanceIndex(path).load_state()

    def copy_related(target: Path) -> None:
        TrainingMaintenanceIndex(target).copy_state(maintenance)

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
            copy_related_records=copy_related,
            fault_injector=interrupt,
        )
    assert path.read_bytes() == corrupt_bytes

    HistoryControlIndex.rebuild(
        path,
        inputs,
        copy_related_records=copy_related,
    )

    rebuilt = HistoryControlIndex(path, reference_path=(path).parent / "company_st.sqlite3")
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
