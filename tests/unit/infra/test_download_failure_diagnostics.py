"""Failure evidence survives retry/restart without becoming pending business state."""

import json
import sqlite3
from dataclasses import replace

import pytest

from tests.helpers.configuration import SqlitePolicyFixture
from tests.helpers.qfq import QFQ_CONFIGURATION, QFQ_TENCENT_OPTIONS
from tests.unit.infra.test_tencent_history_supplier import _Http, DAY, SECURITY
from tests.unit.infra.research.test_history_sync import NOW
from trader.download.domain.download_failure import DownloadFailureContext
from trader.download.infra.download_failure_diagnostics import DownloadFailureDiagnostics, ReadDownloadFailures
from trader.download.infra.qfq_sqlite import SQLiteQfqWindowCache
from trader.download.infra.tencent_daily import TencentDailyQuery
from trader.download.infra.tencent_qfq_supplier import TencentQfqDependencies, TencentQfqSupplier
from trader.infra.http_contracts import HttpCancelled, HttpTimeout
from trader.infra.sqlite_connection import SqliteSession, SqliteConnectionOptions, SqliteTransaction


def _recorder(path):
    cache = SQLiteQfqWindowCache(path, "v2", sqlite_policy=SqlitePolicyFixture())
    return DownloadFailureDiagnostics(cache.RecordDownloadFailure, lambda: NOW)


def _read(path):
    with SqliteSession(path, SqliteConnectionOptions(read_only=True)) as connection:
        return ReadDownloadFailures(connection)


def test_evidence_survives_restart_with_safe_cause_chain_and_distinct_run(tmp_path):
    path = tmp_path / "daily.sqlite3"
    recorder = _recorder(path)
    try:
        try:
            raise sqlite3.OperationalError("secret-token /private/database.sqlite3")
        except sqlite3.Error as exc:
            raise RuntimeError("qfq_pair_database_busy") from exc
    except RuntimeError as exc:
        recorder(DownloadFailureContext("paired_publication", code="600001", elapsed_seconds=1.25), exc)
    _recorder(path)(DownloadFailureContext("publication_validation"), ValueError("private-secret-payload"))
    before = path.read_bytes()
    report = _read(path)
    assert report["total"] == 2
    recent, previous = report["events"]
    assert recent["run_id"] != previous["run_id"]
    assert previous["reason"] == "qfq_pair_database_busy" and previous["elapsed_seconds"] == 1.25
    assert "OperationalError" in previous["causes"] and "test_download_failure_diagnostics.py" in previous["diagnostic"]
    assert "private" not in json.dumps(report) and "secret" not in json.dumps(report)
    assert before == path.read_bytes()
    assert SQLiteQfqWindowCache(path, "v2", sqlite_policy=SqlitePolicyFixture()).price_issues() == ()


@pytest.mark.parametrize("symbol", ("sh600001", "sh000001"))
def test_tencent_failed_attempt_is_durable_even_when_retry_succeeds(tmp_path, monkeypatch, symbol):
    path = tmp_path / "daily.sqlite3"
    http = _Http()
    ticks = iter((0.0, 1.25, 2.0))
    supplier = TencentQfqSupplier(TencentQfqDependencies(
        lambda: http, lambda _: (SECURITY,), lambda: False, failures=_recorder(path), monotonic=lambda: next(ticks)
    ), replace(QFQ_TENCENT_OPTIONS, retries=2))
    attempts = []

    def request(*_args):
        attempts.append(True)
        if len(attempts) == 1:
            raise HttpTimeout("secret-url")
        return ()

    monkeypatch.setattr(supplier, "_request_once", request)
    query = TencentDailyQuery(symbol, DAY, DAY, "qfq", 7)
    assert supplier._request_rows(query) == ()
    event, = _read(path)["events"]
    assert event["stage"] == ("tencent_calendar" if symbol == "sh000001" else "tencent_qfq")
    assert event["code"] == ("" if symbol == "sh000001" else "600001")
    assert event["supplier"] == "tencent"
    assert (event["attempt"], event["maximum_attempts"]) == (1, 3)
    assert event["elapsed_seconds"] == 1.25
    assert event["window_start"] == DAY.isoformat() and event["timeout_seconds"] > 0
    assert len(attempts) == 2


def test_cancellation_and_success_do_not_create_diagnostics(tmp_path):
    path = tmp_path / "daily.sqlite3"
    recorder = _recorder(path)
    recorder(DownloadFailureContext("tencent_qfq"), HttpCancelled("http_cancelled"))
    recorder(DownloadFailureContext("supplier_daily_raw"), RuntimeError("supplier_cancelled"))
    assert not path.exists()


def test_diagnostic_write_failure_is_visible_and_does_not_replace_original_error(caplog):
    def unavailable(_record):
        raise sqlite3.OperationalError("disk full private payload")

    recorder = DownloadFailureDiagnostics(unavailable, lambda: NOW)
    recorder(DownloadFailureContext("paired_publication"), RuntimeError("qfq_pair_database_busy"))
    assert "download_failure_recording_failed" in caplog.text and "private" not in caplog.text


def test_market_reference_preparation_failure_is_recorded_before_workers_start(tmp_path, monkeypatch):
    from trader import bootstrap
    from trader.infra.settings.data_paths import DataPaths
    from tests.helpers.configuration import HistorySyncConfigurationFixture

    paths = DataPaths(tmp_path)
    def prepare(*_args, **_kwargs):
        raise ValueError("history_reference_publication_missing")

    monkeypatch.setattr(bootstrap, "_prepare_reference", prepare)
    with pytest.raises(ValueError, match="history_reference_publication_missing"):
        bootstrap.ExecuteQfqDownload(
            paths, HistorySyncConfigurationFixture(paths.history, paths.reference), QFQ_CONFIGURATION,
            shutdown_timeout_seconds=1, sqlite_policy=SqlitePolicyFixture(), now=lambda: NOW,
        )
    event, = _read(paths.daily_window)["events"]
    assert event["stage"] == "reference_preparation"
    assert event["reason"] == "history_reference_publication_missing"


def test_retention_and_read_only_missing_table(tmp_path):
    path = tmp_path / "daily.sqlite3"
    with SqliteSession(path) as connection:
        assert ReadDownloadFailures(connection)["available"] is False
        assert connection.run("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
    recorder = _recorder(path)
    recorder(DownloadFailureContext("validation"), ValueError("invalid"))
    with SqliteSession(path) as connection, SqliteTransaction(connection, immediate=True):
        connection.run_many(
            "INSERT INTO download_failure SELECT ?,run_id,occurred_at,stage,code,supplier,attempt,maximum_attempts,"
            "elapsed_seconds,timeout_seconds,reason,causes,diagnostic,window_start,window_end,endpoint "
            "FROM download_failure WHERE id=1", ((index,) for index in range(2, 20002)),
        )
    recorder(DownloadFailureContext("latest_validation"), ValueError("latest"))
    report = _read(path)
    assert report["total"] == 20000 and len(report["events"]) == 50
    assert report["events"][0]["stage"] == "latest_validation"


def test_sync_preparation_preserves_original_exception_before_public_result_projection(tmp_path):
    from trader.download.application.sync_data import SyncData

    path = tmp_path / "daily.sqlite3"
    def prepare():
        try:
            raise HttpTimeout("private-request-url")
        except HttpTimeout as exc:
            raise RuntimeError("history_reference_publication_missing") from exc

    def unreachable(_reference):
        raise AssertionError("preparation failed before task dispatch")

    result = SyncData(prepare, unreachable, unreachable, unreachable, lambda: False, failures=_recorder(path)).execute("market")
    assert result.failure == "preparation_RuntimeError"
    event, = _read(path)["events"]
    assert event["reason"] == "history_reference_publication_missing"
    assert "HttpTimeout" in event["causes"] and event["stage"] == "reference_preparation"
