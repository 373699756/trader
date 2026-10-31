from __future__ import annotations

from tests.helpers.configuration import SqlitePolicyFixture

import hashlib
import json
import sqlite3
from dataclasses import replace

import pytest

from scripts.runtime_diagnostics.qfq_sqlite import AuditQfqDatabase, ClassifyQfqDiagnosticError
from tests.helpers.qfq import QFQ_CONFIGURATION, qfq_window
from trader.download.infra.qfq_sqlite import SQLiteQfqWindowCache
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction


def _seed(path, *, complete=True):
    primary, peer = SQLiteQfqWindowCache(path, "v2", sqlite_policy=SqlitePolicyFixture()), SQLiteQfqWindowCache(path, "v3", sqlite_policy=SqlitePolicyFixture())
    window = qfq_window("600001")
    window = replace(window, cells=window.cells[-251:])
    days = tuple(cell.trade_date for cell in window.cells)
    primary.replace_paired_windows(
        peer,
        (window,),
        "fixture",
        QFQ_CONFIGURATION,
        calendar=days,
        completed_day=days[-1] if complete else None,
    )


def test_audit_reads_shared_pair_and_checkpoint_without_changes(tmp_path):
    path = tmp_path / "daily_window.sqlite3"
    _seed(path)
    before = hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns
    report = AuditQfqDatabase(path, sqlite_policy=SqlitePolicyFixture())
    assert report["status"] == "passed"
    assert report["summary"]["mode"] == "shared_daily_window"
    assert report["summary"]["source_data_writes"] == 0
    assert report["summary"]["databases"] == 1
    assert report["summary"]["database_not_checkpointed"] == 0
    assert "600001" not in json.dumps(report)
    assert before == (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)


def test_audit_reports_unconfirmed_publication(tmp_path):
    path = tmp_path / "daily_window.sqlite3"
    _seed(path, complete=False)
    report = AuditQfqDatabase(path, sqlite_policy=SqlitePolicyFixture())
    assert report["status"] == "degraded"
    assert report["summary"]["database_not_checkpointed"] == 1


def test_audit_reports_checkpoint_source_conflict(tmp_path):
    path = tmp_path / "daily_window.sqlite3"
    _seed(path)
    with SqliteSession(path) as connection, SqliteTransaction(connection):
        connection.run("UPDATE window_completion SET source='other'")
    report = AuditQfqDatabase(path, sqlite_policy=SqlitePolicyFixture())
    assert report["status"] == "degraded"
    assert report["summary"]["checkpoint_not_paired"] == 1


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / "missing.sqlite3"
    with pytest.raises(FileNotFoundError):
        AuditQfqDatabase(path, sqlite_policy=SqlitePolicyFixture())
    assert not path.exists()


def test_busy_reason_is_stable():
    assert ClassifyQfqDiagnosticError(sqlite3.OperationalError("database is locked")) == "qfq_pair_database_busy"
