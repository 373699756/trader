
from tests.helpers.configuration import SqlitePolicyFixture
import sqlite3
from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction
from trader.training.application.historical_screening import HistoricalSecurity
from trader.training.domain.evaluation.h1_point_in_time import H1PointInTimeRecord, H1PointInTimeSpec
from trader.training.domain.evaluation.historical_screening import HistoricalPriceBar
from trader.training.infra.research.h1_point_in_time_snapshot import (
    H1PointInTimeSnapshotConflictError,
    SQLiteH1PointInTimeSnapshot,
)


def _record(close: float = 10.2):
    day = date(2026, 8, 31)
    bar = HistoricalPriceBar(day, 10, close, max(10.3, close), 9.9, 100, 1000, 2, None, "qfq", "fixture")
    digest = "c" * 64
    return H1PointInTimeRecord(
        "tomorrow",
        "600001",
        day,
        datetime.fromisoformat("2026-08-31T15:00:00+08:00"),
        bar,
        10.1,
        50,
        500,
        digest,
        digest,
        digest,
    )


def test_h1_snapshot_is_idempotent_and_detects_tampering(tmp_path):
    snapshot = SQLiteH1PointInTimeSnapshot(tmp_path, sqlite_policy=SqlitePolicyFixture())
    spec = H1PointInTimeSpec("tomorrow")
    snapshot.register_universe(spec, (HistoricalSecurity("600001", "main", "A", False, False),))
    snapshot.save_records(spec, "600001", (_record(),))
    snapshot.save_records(spec, "600001", (_record(),))
    manifest = snapshot.manifest(spec)
    assert manifest.spec_hash == spec.content_hash
    assert manifest.completed_codes == 1
    assert snapshot.completed_codes("tomorrow") == frozenset({"600001"})
    with (
        SqliteSession(tmp_path / "experiments/h1_point_in_time/samples.sqlite3") as connection,
        SqliteTransaction(connection),
    ):
        connection.run("UPDATE h1_price SET close = 10.7 WHERE code = '600001'")
    with pytest.raises(H1PointInTimeSnapshotConflictError, match="payload"):
        snapshot.manifest(spec)


def test_h1_snapshot_audit_marks_insufficient_without_opening_holdout(tmp_path):
    snapshot = SQLiteH1PointInTimeSnapshot(tmp_path, sqlite_policy=SqlitePolicyFixture())
    audit = snapshot.audit(H1PointInTimeSpec("d25"))
    assert audit.manifest.state == "historical_data_insufficient"
    assert audit.terminal_holdout_opened is False


def test_h1_snapshot_preserves_utc_observation_identity(tmp_path):
    snapshot = SQLiteH1PointInTimeSnapshot(tmp_path, sqlite_policy=SqlitePolicyFixture())
    spec = H1PointInTimeSpec("tomorrow")
    snapshot.register_universe(spec, (HistoricalSecurity("600001", "main", "A", False, False),))
    original = _record()
    record = replace(original, observed_at=original.observed_at.astimezone(timezone.utc))
    snapshot.save_records(spec, "600001", (record,))

    manifest = snapshot.manifest(spec)
    assert manifest.completed_codes == 1
    snapshot.save_records(spec, "600001", (record,))
    assert snapshot.manifest(spec) == manifest


def test_h1_reader_rejects_unsupported_schema_without_writing(tmp_path):
    snapshot = SQLiteH1PointInTimeSnapshot(tmp_path, sqlite_policy=SqlitePolicyFixture())
    spec = H1PointInTimeSpec("tomorrow")
    snapshot.register_universe(spec, (HistoricalSecurity("600001", "main", "A", False, False),))
    database = tmp_path / "experiments/h1_point_in_time/samples.sqlite3"
    with SqliteSession(database) as connection:
        connection.run("PRAGMA user_version=0")
    before = database.read_bytes()

    with pytest.raises(sqlite3.DatabaseError, match="schema_unsupported"):
        snapshot.registered_universe("tomorrow")
    assert database.read_bytes() == before


def test_h1_snapshot_rejects_retired_spec_identity_without_migration(tmp_path):
    snapshot = SQLiteH1PointInTimeSnapshot(tmp_path, sqlite_policy=SqlitePolicyFixture())
    spec = H1PointInTimeSpec("tomorrow")
    snapshot.register_universe(spec, (HistoricalSecurity("600001", "main", "A", False, False),))
    database = tmp_path / "experiments/h1_point_in_time/samples.sqlite3"
    with SqliteSession(database) as connection, SqliteTransaction(connection):
        connection.run("UPDATE h1_spec SET spec_hash = ? WHERE strategy = 'tomorrow'", (bytes.fromhex("f" * 64),))

    with pytest.raises(H1PointInTimeSnapshotConflictError, match="spec"):
        snapshot.manifest(spec)


def test_h1_snapshot_rejects_direct_identity_mismatch_and_universe_tampering(tmp_path):
    snapshot = SQLiteH1PointInTimeSnapshot(tmp_path, sqlite_policy=SqlitePolicyFixture())
    spec = H1PointInTimeSpec("tomorrow")
    snapshot.register_universe(spec, (HistoricalSecurity("600001", "main", "A", False, False),))
    with pytest.raises(ValueError, match="identity"):
        snapshot.save_records(spec, "600002", (_record(),))
    database = tmp_path / "experiments/h1_point_in_time/samples.sqlite3"
    with SqliteSession(database) as connection, SqliteTransaction(connection):
        connection.run("UPDATE h1_universe SET name = 'tampered' WHERE code = '600001'")
    with pytest.raises(H1PointInTimeSnapshotConflictError, match="universe payload"):
        snapshot.manifest(spec)
