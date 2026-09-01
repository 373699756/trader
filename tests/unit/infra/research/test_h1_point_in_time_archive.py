import sqlite3
from datetime import date, datetime

import pytest

from trader.application.research.historical_screening import HistoricalSecurity
from trader.domain.research.h1_point_in_time import H1PointInTimeRecord, H1PointInTimeSpec
from trader.domain.research.historical_screening import HistoricalPriceBar
from trader.infra.research.h1_point_in_time_archive import H1ArchiveConflictError, SQLiteH1PointInTimeArchive


def _record(close: float = 10.2):
    day = date(2026, 8, 31)
    bar = HistoricalPriceBar(day, 10, close, max(10.3, close), 9.9, 100, 1000, 2, None, "qfq", "fixture")
    digest = "c" * 64
    return H1PointInTimeRecord(
        "today",
        "600001",
        day,
        datetime.fromisoformat("2026-08-31T11:20:00+08:00"),
        bar,
        10.1,
        50,
        500,
        digest,
        digest,
        digest,
    )


def test_h1_archive_is_idempotent_and_detects_tampering(tmp_path):
    archive = SQLiteH1PointInTimeArchive(tmp_path)
    spec = H1PointInTimeSpec("today")
    archive.register_universe(spec, (HistoricalSecurity("600001", "main", "A", False, False),))
    archive.save_records(spec, "600001", (_record(),))
    archive.save_records(spec, "600001", (_record(),))
    manifest = archive.manifest(spec)
    assert manifest.spec_hash == spec.content_hash
    assert manifest.completed_codes == 1
    assert archive.completed_codes("today") == frozenset({"600001"})
    with sqlite3.connect(tmp_path / "score-h1-point-in-time" / "score-h1-point-in-time.sqlite3") as connection:
        connection.execute("UPDATE records SET close_price = 10.7 WHERE code = '600001'")
    with pytest.raises(H1ArchiveConflictError, match="payload"):
        archive.manifest(spec)


def test_h1_archive_audit_marks_insufficient_without_opening_holdout(tmp_path):
    archive = SQLiteH1PointInTimeArchive(tmp_path)
    audit = archive.audit(H1PointInTimeSpec("d25"))
    assert audit.manifest.state == "historical_data_insufficient"
    assert audit.terminal_holdout_opened is False


def test_h1_archive_rejects_direct_identity_mismatch_and_universe_tampering(tmp_path):
    archive = SQLiteH1PointInTimeArchive(tmp_path)
    spec = H1PointInTimeSpec("today")
    archive.register_universe(spec, (HistoricalSecurity("600001", "main", "A", False, False),))
    with pytest.raises(ValueError, match="identity"):
        archive.save_records(spec, "600002", (_record(),))
    database = tmp_path / "score-h1-point-in-time" / "score-h1-point-in-time.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE universe SET name = 'tampered' WHERE code = '600001'")
    with pytest.raises(H1ArchiveConflictError, match="universe payload"):
        archive.manifest(spec)
