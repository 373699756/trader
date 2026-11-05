from __future__ import annotations

from scripts.checks.catalog import SelectionArguments

from tests.helpers.configuration import DownloadFailureConfigurationFixture, SqlitePolicyFixture, PROJECT_ROOT
from functools import partial
import shutil

import hashlib
import json
import sqlite3
from dataclasses import replace

import pytest

from scripts.checks.download.history.qfq_sqlite import AuditQfqDatabase, ClassifyQfqDiagnosticError
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
    # Test the explicit logical audit after a valid physical publication.
    SQLiteQfqWindowCache(path, "v2", sqlite_policy=SqlitePolicyFixture()).publish_file()
    report = AuditQfqDatabase(path, sqlite_policy=SqlitePolicyFixture())
    assert report["status"] == "degraded"
    assert report["summary"]["checkpoint_not_paired"] == 1


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / "missing.sqlite3"
    with pytest.raises(FileNotFoundError):
        AuditQfqDatabase(path, sqlite_policy=SqlitePolicyFixture())
    assert not path.exists()


def test_audit_locates_latest_absent_prices_without_claiming_confirmed_suspension(tmp_path):
    path = tmp_path / "daily_window.sqlite3"
    primary = SQLiteQfqWindowCache(path, "v2", sqlite_policy=SqlitePolicyFixture())
    peer = SQLiteQfqWindowCache(path, "v3", sqlite_policy=SqlitePolicyFixture())
    window = qfq_window("600001")
    days = tuple(cell.trade_date for cell in window.cells[-251:])
    window = replace(window, cells=tuple(
        replace(cell, status="tencent_inferred_suspended", unadjusted=None, qfq=None)
        if cell.trade_date in days[-2:] else cell
        for cell in window.cells[-251:]
    ))
    primary.replace_paired_windows(
        peer, (window,), "fixture", QFQ_CONFIGURATION, calendar=days, completed_day=days[-1]
    )
    before = hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns

    report = AuditQfqDatabase(path, sqlite_policy=SqlitePolicyFixture())

    assert report["status"] == "passed"
    assert report["summary"]["latest_without_prices"] == [{
        "code": "600001",
        "cutoff": days[-1].isoformat(),
        "last_price_date": days[-3].isoformat(),
        "nontrading_dates": tuple(day.isoformat() for day in days[-2:]),
        "nontrading_is_not_exchange_confirmation": True,
    }]
    assert before == (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)


def test_busy_reason_is_stable():
    assert ClassifyQfqDiagnosticError(sqlite3.OperationalError("database is locked")) == "qfq_pair_database_busy"


def test_valid_pair_does_not_hide_current_price_issues(tmp_path):
    from trader.download.domain.price_repair import PriceIssue

    path = tmp_path / "daily_window.sqlite3"
    _seed(path)
    cache = SQLiteQfqWindowCache(path, "v2", sqlite_policy=SqlitePolicyFixture())
    day = qfq_window("600001").cells[-1].trade_date
    # A newly failing member has no binding or completion to mismatch; the
    # previously published member must remain valid while the issue is reported.
    cache.record_price_issue(PriceIssue("600002", (day,), "timeout"))
    report = AuditQfqDatabase(path, sqlite_policy=SqlitePolicyFixture())
    assert report["status"] == "degraded"
    assert report["summary"]["pending_stocks"] == 1
    assert report["summary"]["pending_days"] == 1
    assert report["summary"]["paired_identity_mismatches"] == 0
    assert report["summary"]["database_not_checkpointed"] == 0
    assert report["summary"]["checkpoint_not_paired"] == 0


def test_failure_profile_reads_only_diagnostic_rows_and_survives_bad_price_content(tmp_path, monkeypatch, capsys):
    from scripts.checks.arguments import BuildParser
    from scripts.checks.arguments import ValidateOptions
    from scripts.checks.commands import BuildCommands
    from scripts.checks.projections import BuildReport
    from scripts.checks.contracts import DiagnosticResult
    from scripts.checks.download.history import qfq_sqlite
    from trader.download.domain.download_failure import DownloadFailureContext
    from trader.download.infra.download_failure_diagnostics import DownloadFailureDiagnostics
    from tests.unit.infra.research.test_history_sync import NOW
    import sys

    path = tmp_path / "daily_window.sqlite3"
    _seed(path)
    primary = SQLiteQfqWindowCache(path, "v2", sqlite_policy=SqlitePolicyFixture())
    DownloadFailureDiagnostics(partial(primary.RecordDownloadFailure, configuration=DownloadFailureConfigurationFixture()), lambda: NOW)(
        DownloadFailureContext("tencent_qfq", code="600001"), RuntimeError("tencent_qfq_request_failed")
    )
    # A broken business binding must not prevent reading why the update failed.
    with SqliteSession(path) as connection, SqliteTransaction(connection):
        connection.run("UPDATE window_binding SET content_hash=?", (b"x" * 32,))
    before = path.read_bytes()
    config = tmp_path / "config"
    shutil.copytree(PROJECT_ROOT / "config", config)
    document = json.loads((config / "download.json").read_text())
    document["failure_diagnostics"] = {"retention_limit": 7, "query_limit": 2}
    (config / "download.json").write_text(json.dumps(document))
    options, _ = ValidateOptions(BuildParser().parse_args([
        *SelectionArguments('download-failures'), "--qfq-database", str(path), "--runtime-config", str(config / "runtime.json"),
    ]))
    command, = BuildCommands(options)
    assert "--failures-only" in command.argv and str(path) in command.argv
    monkeypatch.setattr(sys, "argv", ["qfq_sqlite", *command.argv[command.argv.index("--") + 1:]])
    assert qfq_sqlite.Main() == 0
    output = json.loads(capsys.readouterr().out)
    report = BuildReport("download-failures", [DiagnosticResult(command.name, 0, 1, output, None)])
    assert report["checks"][0]["summary"]["events"][0]["code"] == "600001"
    assert report["checks"][0]["summary"]["retention_limit"] == 7
    assert report["checks"][0]["summary"]["query_limit"] == 2
    assert before == path.read_bytes()
