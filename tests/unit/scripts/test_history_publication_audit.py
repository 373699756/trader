from __future__ import annotations

import hashlib
import json
from datetime import date

import pytest

from scripts.diagnose_runtime import DiagnosticResult, _parser, _validate, build_commands, build_report
from scripts.runtime_diagnostics.history_publication_audit import AuditHistoryPublication
from tests.helpers.configuration import HistorySyncConfigurationFixture, SqlitePolicyFixture
from tests.helpers.history import RunHistoryFixture
from tests.unit.infra.research.test_history_sync import FakeSupplier, NOW
from trader.download.infra.history_control_index import HistoryControlIndex
from trader.download.infra.history_prices import HistoryPricePartitionError
from trader.infra.sqlite_codec import EncodeDate
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction


def _seed(root):
    configuration = HistorySyncConfigurationFixture(
        root, sessions=3, reread_sessions=2, minimum_free_bytes=0, reference_root=root.parent / "reference"
    )
    days = (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))
    assert RunHistoryFixture(configuration, FakeSupplier(days), clock=lambda: NOW).state == "completed"


def test_complete_history_is_verified_without_mutation_and_projected(tmp_path):
    root = tmp_path / "history"
    _seed(root)
    paths = tuple(root.glob("*.sqlite3"))
    before = tuple((p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).digest()) for p in paths)
    report = AuditHistoryPublication(root, sqlite_policy=SqlitePolicyFixture())
    assert report["status"] == "passed"
    assert report["summary"]["verified_rows"] == 6
    assert report["summary"]["stocks"] == 2
    assert report["summary"]["receipts"]["gap_days"] == 0
    assert before == tuple((p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).digest()) for p in paths)
    assert "600001" not in json.dumps(report)
    options, _ = _validate(_parser().parse_args([
        "--profile", "history-publication", "--history-root", str(root),
    ]))
    command, = build_commands(options)
    assert "scripts.runtime_diagnostics.history_publication_audit" in command.argv
    assert str(root) in command.argv
    projected = build_report("history-publication", [DiagnosticResult(command.name, 0, 1, report, None)])
    assert projected["checks"][0]["summary"]["verified_rows"] == 6


@pytest.mark.parametrize(("sql", "reason"), [
    ("UPDATE download_batch SET sync_sequence=sync_sequence+1", "history_batch_publication_mismatch"),
    ("UPDATE download_batch SET cutoff_date=cutoff_date-1", "history_batch_publication_mismatch"),
    ("UPDATE download_tasks SET tencent_done=0", "history_tasks_unfinished"),
    ("DELETE FROM download_tasks", "history_task_membership_mismatch"),
])
def test_published_content_does_not_hide_unfinished_work(tmp_path, sql, reason):
    _seed(tmp_path)
    with SqliteSession(tmp_path / "catalog.sqlite3") as connection, SqliteTransaction(connection):
        connection.run(sql)
    report = AuditHistoryPublication(tmp_path, sqlite_policy=SqlitePolicyFixture())
    assert report["status"] == "degraded"
    assert reason in report["summary"]["pending"]


def test_price_gap_and_failed_repair_remain_pending(tmp_path):
    _seed(tmp_path)
    day = EncodeDate(date(2026, 9, 10))
    with SqliteSession(tmp_path / "catalog.sqlite3") as connection, SqliteTransaction(connection):
        connection.run("INSERT INTO download_gaps VALUES(?,?)", ("600001", day))
        connection.run(
            "INSERT INTO price_repair_attempt VALUES(?,?,'fixture','failed','timeout',NULL)", ("600001", day)
        )
    report = AuditHistoryPublication(tmp_path, sqlite_policy=SqlitePolicyFixture())
    assert report["status"] == "degraded"
    assert report["summary"]["receipts"]["unresolved_repair_days"] == 1
    assert "history_price_gaps" in report["summary"]["pending"]


def test_media_content_corruption_is_rejected(tmp_path):
    _seed(tmp_path)
    with SqliteSession(tmp_path / "prices_2026.sqlite3") as connection, SqliteTransaction(connection):
        connection.run("UPDATE daily_price_revision SET content_hash=?", (b"x" * 32,))
    with pytest.raises(HistoryPricePartitionError):
        AuditHistoryPublication(tmp_path, sqlite_policy=SqlitePolicyFixture())


def test_changed_publication_cannot_pass(tmp_path, monkeypatch):
    _seed(tmp_path)
    original = HistoryControlIndex.load_published_state
    calls = 0

    def load(self):
        nonlocal calls
        calls += 1
        return original(self) if calls == 1 else None

    monkeypatch.setattr(HistoryControlIndex, "load_published_state", load)
    report = AuditHistoryPublication(tmp_path, sqlite_policy=SqlitePolicyFixture())
    assert report["status"] == "failed"
    assert report["summary"]["failures"] == ["history_changed_during_audit"]


def test_missing_database_is_not_initialized(tmp_path):
    with pytest.raises(FileNotFoundError):
        AuditHistoryPublication(tmp_path, sqlite_policy=SqlitePolicyFixture())
    assert not tuple(tmp_path.iterdir())
