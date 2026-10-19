from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import replace

import pytest

from scripts.runtime_diagnostics.qfq_sqlite import AuditActiveQfq, ClassifyQfqDiagnosticError
from tests.helpers.qfq import qfq_window
from trader.download.infra.qfq_sqlite import SQLiteQfqWindowCache
from trader.infra.sqlite_connection import OpenSqliteConnection


def _seed(path, *, complete=True):
    primary, peer = SQLiteQfqWindowCache(path, "v2"), SQLiteQfqWindowCache(path, "v3")
    window = qfq_window("600001")
    window = replace(window, cells=window.cells[-251:])
    days = tuple(cell.trade_date for cell in window.cells)
    primary.replace_paired_windows(
        peer, (window,), "fixture", calendar=days, completed_day=days[-1] if complete else None
    )


def test_audit_reads_shared_pair_and_checkpoint_without_changes(tmp_path):
    path = tmp_path / "daily_window.sqlite3"
    _seed(path)
    before = hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns
    report = AuditActiveQfq(path)
    assert report["status"] == "passed"
    assert report["summary"]["mode"] == "shared_daily_window"
    assert report["summary"]["active_data_writes"] == 0
    assert report["summary"]["databases"] == 1
    assert report["summary"]["database_not_checkpointed"] == 0
    assert "600001" not in json.dumps(report)
    assert before == (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)


def test_audit_reports_unconfirmed_publication(tmp_path):
    path = tmp_path / "daily_window.sqlite3"
    _seed(path, complete=False)
    report = AuditActiveQfq(path)
    assert report["status"] == "degraded"
    assert report["summary"]["database_not_checkpointed"] == 1


def test_audit_reports_checkpoint_source_conflict(tmp_path):
    path = tmp_path / "daily_window.sqlite3"
    _seed(path)
    with closing(OpenSqliteConnection(path)) as connection, connection:
        connection.run("UPDATE completions SET source='other'")
    report = AuditActiveQfq(path)
    assert report["status"] == "degraded"
    assert report["summary"]["checkpoint_not_paired"] == 1


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / "missing.sqlite3"
    with pytest.raises(FileNotFoundError):
        AuditActiveQfq(path)
    assert not path.exists()


def test_busy_reason_is_stable():
    assert ClassifyQfqDiagnosticError(sqlite3.OperationalError("database is locked")) == "qfq_pair_database_busy"
