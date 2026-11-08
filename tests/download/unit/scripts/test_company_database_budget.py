"""Only this audit's observed deadline cancellation may explain an SQLite interrupt."""

import json
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

import pytest

from scripts.core import execution
from scripts.core.cli import Main as ToolMain
from scripts.download.company import company_publication_audit, company_query_audit, company_resume
from tests.helpers.download.company import company_evidence as fixtures
from tests.helpers.shared.configuration import SqlitePolicyFixture
from trader.download.domain.company_evidence import DisclosurePage
from trader.infra.sqlite_connection import SqliteConnection, SqliteSession


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def _budget_failure(monkeypatch, *, inventory=False):
    clock = _Clock()
    monkeypatch.setattr(company_query_audit.time, "monotonic", clock)

    def interrupted(path, *, now, deadline, cancel_requested, facts_path, sqlite_policy, risk_baseline):
        clock.now = deadline
        assert cancel_requested()
        if inventory:
            raise company_query_audit._CompanyDatabaseAuditBudgetExceeded("company_database_audit_budget_exhausted")
        # Constructed errors have no Python 3.11 sqlite_errorcode attribute.
        raise sqlite3.OperationalError("interrupted")

    monkeypatch.setattr(company_query_audit, "_audit_company_database", interrupted)
    return company_query_audit.AuditCompanyDatabase(
        Path("unused.sqlite3"), now=fixtures.NOW, budget_seconds=30, sqlite_policy=SqlitePolicyFixture(),
    )


def test_real_sqlite_progress_interrupt_during_dbstat_produces_a_failed_budget_report(tmp_path, monkeypatch):
    clock = _Clock()
    attempted = []
    original_run = SqliteConnection.run
    monkeypatch.setattr(company_query_audit.time, "monotonic", clock)

    @contextmanager
    def memory_session(path, options, *, cancel_requested, sqlite_policy):
        assert options.read_only
        with SqliteSession(":memory:", cancel_requested=cancel_requested, sqlite_policy=sqlite_policy) as connection:
            yield connection

    def run(connection, sql, parameters=()):
        if "FROM dbstat" in sql:
            attempted.append(sql)
            clock.now = 30
            # Force genuine SQLite progress callbacks without a large database.
            return original_run(connection, (
                "WITH RECURSIVE sample(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM sample WHERE n<100000) "
                "SELECT SUM(n) FROM sample"
            ))
        return original_run(connection, sql, parameters)

    monkeypatch.setattr(company_query_audit, "SqliteSession", memory_session)
    monkeypatch.setattr(SqliteConnection, "run", run)
    result = company_query_audit.AuditCompanyDatabase(
        tmp_path / "not-opened.sqlite3", now=fixtures.NOW, budget_seconds=30, sqlite_policy=SqlitePolicyFixture(),
    )
    assert len(attempted) == 1
    assert result["status"] == "failed"
    database = result["summary"]["database"]
    assert database == {
        "read_only": True, "complete": False, "budget_exhausted": True,
        "failure_category": "budget_exhausted", "failure_code": "company_database_audit_budget_exhausted",
        "field_correctness_verified": False, "full_market_verified": False,
    }
    assert result["findings"] == ["company_database_audit_budget_exhausted"]
    assert "WITH RECURSIVE" not in json.dumps(result) and str(tmp_path) not in json.dumps(result)


def test_retry_inventory_deadline_produces_the_same_failed_budget_report(tmp_path, monkeypatch):
    index = fixtures._index(tmp_path)
    partition = index.partitions(("sse",), fixtures.NOW)[0]
    item = fixtures._disclosure(title="关于收到立案告知书的公告")
    index.commit_page(partition, DisclosurePage((item,), 1, False), fixtures.NOW)
    index.document_failure(
        item, "fixture", "disclosure_document_no_text", fixtures.NOW, fixtures.NOW + timedelta(days=1),
    )
    before = index.path.read_bytes()
    clock = _Clock()
    original_run = SqliteConnection.run
    monkeypatch.setattr(company_query_audit.time, "monotonic", clock)

    def run(connection, sql, parameters=()):
        result = original_run(connection, sql, parameters)
        if "r.retry_policy_id" in sql and "FROM document_retries r JOIN disclosures d" in sql:
            rows = result.fetchall()
            assert len(rows) == 1
            # SQL completed before the deadline; only the real Python inventory loop expires.
            clock.now = 30
            return iter(rows)
        return result

    monkeypatch.setattr(SqliteConnection, "run", run)
    result = company_query_audit.AuditCompanyDatabase(
        index.path, now=fixtures.NOW, budget_seconds=30, sqlite_policy=SqlitePolicyFixture(),
    )
    assert result["status"] == "failed"
    database = result["summary"]["database"]
    assert database["failure_code"] == "company_database_audit_budget_exhausted"
    assert database["budget_exhausted"] and not database["complete"]
    assert not database["field_correctness_verified"] and not database["full_market_verified"]
    assert index.path.read_bytes() == before


@pytest.mark.parametrize(("message", "expired", "callback"), (
    ("interrupted", True, False),
    ("interrupted", False, True),
    ("no such table: private_input", True, True),
))
def test_unproven_interrupt_and_other_sqlite_errors_keep_their_original_cause(
    monkeypatch, message, expired, callback,
):
    clock = _Clock()
    error = sqlite3.OperationalError(message)
    cause = ValueError("private cause remains on the exception")
    monkeypatch.setattr(company_query_audit.time, "monotonic", clock)

    def failed(path, *, now, deadline, cancel_requested, facts_path, sqlite_policy, risk_baseline):
        clock.now = deadline if expired else deadline - 1
        if callback:
            assert cancel_requested() is expired
        raise error from cause

    monkeypatch.setattr(company_query_audit, "_audit_company_database", failed)
    with pytest.raises(sqlite3.OperationalError) as captured:
        company_query_audit.AuditCompanyDatabase(
            Path("unused.sqlite3"), now=fixtures.NOW, budget_seconds=30, sqlite_policy=SqlitePolicyFixture(),
        )
    assert captured.value is error and captured.value.__cause__ is cause


@pytest.mark.parametrize("origin", ("source", "file", "network"))
def test_unrelated_timeout_keeps_its_identity_and_cause_even_after_deadline_cancellation(monkeypatch, origin):
    clock = _Clock()
    error = TimeoutError("company_failure_inventory_timeout")
    cause = OSError(f"private {origin} timeout cause")
    monkeypatch.setattr(company_query_audit.time, "monotonic", clock)

    def failed(path, *, now, deadline, cancel_requested, facts_path, sqlite_policy, risk_baseline):
        clock.now = deadline
        assert cancel_requested()
        raise error from cause

    monkeypatch.setattr(company_query_audit, "_audit_company_database", failed)
    with pytest.raises(TimeoutError) as captured:
        company_query_audit.AuditCompanyDatabase(
            Path("unused.sqlite3"), now=fixtures.NOW, budget_seconds=30, sqlite_policy=SqlitePolicyFixture(),
        )
    assert captured.value is error and captured.value.__cause__ is cause


def test_publication_registers_the_same_caller_budget_for_both_snapshots(tmp_path, monkeypatch):
    index = fixtures._index(tmp_path)
    registered = []
    original_session = company_publication_audit.SqliteSession

    def cancel_requested():
        return False

    @contextmanager
    def session(path, options, *, cancel_requested, sqlite_policy):
        registered.append(cancel_requested)
        with original_session(path, options, cancel_requested=cancel_requested, sqlite_policy=sqlite_policy) as connection:
            yield connection

    monkeypatch.setattr(company_publication_audit, "SqliteSession", session)
    company_publication_audit.AuditCompanyPublication(
        index.path, index.facts_path, now=fixtures.NOW, deadline=time.monotonic() + 10,
        sqlite_policy=SqlitePolicyFixture(), cancel_requested=cancel_requested,
    )
    assert registered == [cancel_requested, cancel_requested]


@pytest.mark.parametrize("inventory", (False, True), ids=("sqlite", "inventory"))
def test_native_and_public_workers_keep_budget_failure_json_and_nonzero_status(tmp_path, monkeypatch, capsys, inventory):
    report = _budget_failure(monkeypatch, inventory=inventory)
    source = tmp_path / "private-input.sqlite3"
    source.touch()
    monkeypatch.setattr(company_resume, "AuditCompanyDatabase", lambda *args, **kwargs: report)
    monkeypatch.setattr(company_resume, "_compact_database", lambda *args, **kwargs: pytest.fail("failed audit compacted"))
    monkeypatch.setattr(sys, "argv", [
        "company-resume", "--database", str(source), "--database-only", "--budget-seconds", "30",
        "--directory", str(tmp_path / "isolated"),
    ])
    native_exit = company_resume.Main()
    native_json = capsys.readouterr().out
    assert native_exit == 1 and json.loads(native_json) == report

    def bounded_command(argv, *, timeout, environment):
        assert "--database-only" in argv and timeout == 60
        return subprocess.CompletedProcess(argv, native_exit, native_json, "")

    monkeypatch.setattr(execution, "RunBoundedCommand", bounded_command)
    assert ToolMain([
        "--business", "download", "--module", "company", "--action", "coverage",
        "--company-database-audit", "--company-audit-budget-seconds", "30",
        "--command-timeout-seconds", "60", "--report", "-",
    ]) == 1
    public = json.loads(capsys.readouterr().out)
    check, = public["checks"]
    assert public["status"] == check["status"] == "failed"
    assert "error_code" not in check  # The worker returned valid JSON, not invalid_json.
    database = check["summary"]["database"]
    for key, value in report["summary"]["database"].items():
        assert database[key] == value
    assert str(source) not in json.dumps(public) and "private cause" not in json.dumps(public)
