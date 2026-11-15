from concurrent.futures import CancelledError
from dataclasses import replace
from datetime import datetime
import json
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tests.helpers.shared.reference_fixtures import PreparedFixture
from trader.download.application.prepare_reference import ReferenceDatabaseIncompatibleError
from trader.download.application.sync_data import SyncData, SyncDatabaseError, SyncResult, SyncTaskResult
from trader.download.entrypoints.sync_progress import SyncProgress
from trader.download.domain.company_evidence import CompanyDatabaseIncompatibleError, CompanyEvidenceSyncResult
from trader.download.domain.history_maintenance import HistoryMaintenanceStatus
from trader.download.domain.qfq_window import QfqUpdateResult
from trader.download.entrypoints.sync_projection import ProjectSyncResult
from trader.download.infra.sync_database_failure import AdaptSyncDatabaseFailures
from trader.infra.sqlite_connection import SqliteSession

_NOW = datetime(2026, 10, 16, tzinfo=ZoneInfo("Asia/Shanghai"))


def _history(state="completed", unresolved_price_cells=0):
    return HistoryMaintenanceStatus(
        state, None, Path("history"), "tencent", None, "a" * 64, _NOW.date(), _NOW.date(), unresolved_price_cells
    )




@pytest.mark.parametrize("target", ("all", "history", "market", "company"))
def test_one_prepared_full_reference_is_shared_in_order(target):
    reference = PreparedFixture(_NOW)
    calls = []

    def prepare():
        calls.append("prepare")
        return reference

    def history(value):
        assert value is reference
        calls.append("history")
        return _history()

    def market(value):
        assert value is reference
        calls.append("market")
        return QfqUpdateResult(_NOW.date(), 1)

    def company(value):
        assert value is reference
        calls.append("company")
        return CompanyEvidenceSyncResult(_NOW, 1, 1, 0, 0, 0, 0, (), 0)

    result = SyncData(prepare, history, market, company, lambda: False).execute(target)
    assert calls == ["prepare", *(("history", "market", "company") if target == "all" else (target,))]
    assert result.exit_code == 0
    assert ProjectSyncResult(result)["status"] == "completed"


@pytest.mark.parametrize("failure", (None, "source_unavailable", "cancelled"))
def test_market_shutdown_failure_preserves_the_published_result_and_original_failure(failure):
    market = QfqUpdateResult(
        _NOW.date(), 17, changed_files=("daily_window.sqlite3",), changed_rows=42,
        failure_reason=failure, shutdown_failure="qfq_shutdown_incomplete",
    )

    def forbidden(_reference):
        pytest.fail("market command must not start another task")

    result = SyncData(
        lambda: PreparedFixture(_NOW), forbidden, lambda _reference: market, forbidden, lambda: False,
    ).execute("market")
    task, = result.tasks
    assert task.market is market
    assert task.status == ("cancelled" if failure == "cancelled" else "failed")
    assert task.reason == "qfq_shutdown_incomplete"
    assert result.exit_code == (130 if failure == "cancelled" else 1)
    assert market.needs_retry
    projected = ProjectSyncResult(result)["tasks"][0]
    assert projected["failure_reason"] == failure
    assert projected["shutdown_failure"] == "qfq_shutdown_incomplete"
    assert projected["changed_rows"] == 42 and projected["completed_codes"] == 17
    assert tuple(projected["changed_files"]) == market.changed_files


@pytest.mark.parametrize("pending", (False, True))
def test_failed_or_incomplete_history_continues_market_and_company(pending):
    calls = []

    def history(value):
        if pending:
            return replace(_history(unresolved_price_cells=1), reason="history_price_gaps_remaining")
        raise RuntimeError("history unavailable")

    def market(value):
        calls.append("market")
        return QfqUpdateResult(_NOW.date(), 1)

    def company(value):
        calls.append("company")
        return CompanyEvidenceSyncResult(_NOW, 1, 1, 0, 0, 0, 0, (), 0)

    result = SyncData(lambda: PreparedFixture(_NOW), history, market, company, lambda: False).execute("all")
    assert calls == ["market", "company"]
    assert result.exit_code == 1
    assert result.tasks[0].status == ("pending" if pending else "failed")
    assert result.tasks[0].reason == ("history_price_gaps_remaining" if pending else "RuntimeError")


@pytest.mark.parametrize("failed_task", ("history", "market", "company"))
@pytest.mark.parametrize("error", (sqlite3.OperationalError, sqlite3.IntegrityError))
def test_sqlite_task_failure_preserves_results_and_continues_following_tasks(failed_task, error):
    calls = []
    failures = []
    history = _history()
    market = QfqUpdateResult(_NOW.date(), 17, changed_rows=42)
    company = CompanyEvidenceSyncResult(_NOW, 1, 1, 0, 0, 0, 0, (), 0)

    def synchronize(task, value):
        calls.append(task)
        if task == failed_task:
            raise error("private database detail")
        return value

    result = SyncData(
        lambda: PreparedFixture(_NOW),
        AdaptSyncDatabaseFailures(lambda _: synchronize("history", history)),
        AdaptSyncDatabaseFailures(lambda _: synchronize("market", market)),
        AdaptSyncDatabaseFailures(lambda _: synchronize("company", company)),
        lambda: False,
        lambda context, exc: failures.append((context.stage, exc)),
    ).execute("all")

    assert calls == ["history", "market", "company"]
    assert result.exit_code == 1 and result.failure is None and not result.cancelled
    assert [task.task for task in result.tasks] == calls
    for task in result.tasks:
        assert task.status == ("failed" if task.task == failed_task else "completed")
        assert task.reason == (error.__name__ if task.task == failed_task else None)
    if failed_task != "history":
        assert result.tasks[0].history is history
    if failed_task != "market":
        assert result.tasks[1].market is market
    if failed_task != "company":
        assert result.tasks[2].company is company
    if failed_task == "market":
        assert len(failures) == 1 and failures[0][0] == "market_command"
        assert isinstance(failures[0][1], SyncDatabaseError)
        assert isinstance(failures[0][1].__cause__, error)
    else:
        assert failures == []
    projected = ProjectSyncResult(result)
    assert projected["status"] == "failed" and len(projected["tasks"]) == 3
    assert "private database detail" not in json.dumps(projected)


def test_database_adapter_preserves_the_cause_from_the_shared_connection():
    def read_missing_table():
        with SqliteSession(":memory:") as connection:
            connection.run("SELECT * FROM missing_table")

    with pytest.raises(SyncDatabaseError) as failure:
        AdaptSyncDatabaseFailures(read_missing_table)()
    assert failure.value.category == "OperationalError"
    assert isinstance(failure.value.__cause__, sqlite3.OperationalError)
    assert "missing_table" not in str(failure.value)


@pytest.mark.parametrize("failed_step", ("prepare", "history", "market", "company"))
def test_sync_composition_translates_database_failures_at_every_injected_boundary(tmp_path, monkeypatch, failed_step):
    from tests.helpers.shared.configuration import RuntimeSettingsFixture, SqlitePolicyFixture
    from trader import bootstrap
    from trader.infra.settings.data_paths import DataPaths

    runtime = replace(RuntimeSettingsFixture(), data_paths=DataPaths(tmp_path))
    calls = []

    def execute(step, result):
        calls.append(step)
        if step == failed_step:
            raise sqlite3.OperationalError("private database detail")
        return result

    monkeypatch.setattr(bootstrap, "LoadRuntimeSettings", lambda _: runtime)
    monkeypatch.setattr(bootstrap, "BuildSqlitePolicy", lambda _: SqlitePolicyFixture())
    monkeypatch.setattr(bootstrap, "_utc_now", lambda: _NOW)
    monkeypatch.setattr(bootstrap, "RecentPublishedNewsCodes", lambda *_: ())
    monkeypatch.setattr(bootstrap, "_prepare_reference", lambda *_, **__: execute("prepare", PreparedFixture(_NOW)))
    monkeypatch.setattr(bootstrap, "ExecuteHistoryDownload", lambda *_, **__: execute("history", _history()))
    monkeypatch.setattr(bootstrap, "ExecuteQfqDownload", lambda *_, **__: execute("market", QfqUpdateResult(_NOW.date(), 1)))
    monkeypatch.setattr(bootstrap, "_sync_company_evidence", lambda *_, **__: execute(
        "company", CompanyEvidenceSyncResult(_NOW, 1, 1, 0, 0, 0, 0, (), 0),
    ))
    monkeypatch.setattr(bootstrap, "DownloadFailureDiagnostics", lambda *_, **__: lambda *_: None)

    result = bootstrap.ExecuteSync(runtime.config_path, "all", report=SyncProgress(lambda _: None, lambda: 0.0))
    assert result.exit_code == 1
    if failed_step == "prepare":
        assert calls == ["prepare"] and result.tasks == ()
        assert result.failure == "preparation_OperationalError"
    else:
        assert calls == ["prepare", "history", "market", "company"]
        assert [task.status for task in result.tasks] == [
            "failed" if task.task == failed_step else "completed" for task in result.tasks
        ]
        assert next(task for task in result.tasks if task.task == failed_step).reason == "OperationalError"
    assert "private database detail" not in json.dumps(ProjectSyncResult(result))


@pytest.mark.parametrize("failure", ("qfq_worker_unavailable", "source_unavailable", "cancelled"))
def test_market_download_failure_reason_reaches_summary_json_and_cli(tmp_path, monkeypatch, capsys, failure):
    market = QfqUpdateResult(_NOW.date(), 17, pending_codes=1, changed_rows=42, failure_reason=failure)

    def forbidden(_):
        pytest.fail("market command must not start another task")

    result = SyncData(
        lambda: PreparedFixture(_NOW), forbidden, lambda _: market, forbidden, lambda: False,
    ).execute("market")
    status = "cancelled" if failure == "cancelled" else "pending"
    assert result.tasks[0].market is market and result.tasks[0].reason == failure
    assert result.tasks[0].status == status
    from tests.helpers.shared.configuration import RuntimeSettingsFixture
    from trader.entrypoints import cli

    runtime = RuntimeSettingsFixture()
    monkeypatch.setattr(cli, "LoadRuntimeSettings", lambda _: runtime)
    monkeypatch.setattr(cli, "_configuration_payload", lambda *_: {})
    monkeypatch.setattr("trader.bootstrap.PrepareRuntimeDisk", lambda settings, **_: settings)
    monkeypatch.setattr("trader.bootstrap.ExecuteSync", lambda *_, **__: result)
    messages = []
    progress = SyncProgress(messages.append, lambda: 0.0)
    args = cli.BuildParser().parse_args(["--config", str(tmp_path / "runtime.json"), "sync", "market"])
    assert cli._run_sync(args, progress) == (130 if status == "cancelled" else 1)
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == status
    assert payload["tasks"][0]["reason"] == payload["tasks"][0]["failure_reason"] == failure
    assert payload["tasks"][0]["changed_rows"] == 42
    terminal = messages[-1]
    if status == "pending":
        assert terminal.state == "等待"
        assert f"market 待补齐 原因 {failure}" in " | ".join(field.text for field in terminal.fields)
    else:
        assert terminal.state == "取消"


def test_sqlite_failure_during_cancellation_keeps_previous_result_and_skips_following_task():
    cancelled = False
    history = _history()

    def interrupt(_):
        nonlocal cancelled
        cancelled = True
        raise sqlite3.OperationalError("interrupted")

    def forbidden(_):
        pytest.fail("company must not start after cancellation")

    result = SyncData(
        lambda: PreparedFixture(_NOW), lambda _: history,
        AdaptSyncDatabaseFailures(interrupt), forbidden, lambda: cancelled,
    ).execute("all")
    assert result.cancelled and result.exit_code == 130 and result.failure is None
    assert [task.status for task in result.tasks] == ["completed", "cancelled"]
    assert result.tasks[0].history is history and result.tasks[1].task == "market"
    assert ProjectSyncResult(result)["status"] == "cancelled"


@pytest.mark.parametrize(("result", "status", "state", "exit_code"), (
    (SyncResult((SyncTaskResult("history", "pending", "history_price_gaps_remaining"),)), "pending", "等待", 1),
    (SyncResult((SyncTaskResult("history", "completed"),)), "completed", "完成", 0),
    (SyncResult((SyncTaskResult("history", "failed", "RuntimeError"),)), "failed", "失败", 1),
    (SyncResult((), failure="preparation_RuntimeError"), "failed", "失败", 1),
    (SyncResult((SyncTaskResult("history", "cancelled"),), cancelled=True), "cancelled", "取消", 130),
    (SyncResult((SyncTaskResult("history", "failed", "RuntimeError"),), cancelled=True), "cancelled", "取消", 130),
))
def test_cli_summary_keeps_pending_distinct_from_failure_and_preserves_json_and_exit_code(
    tmp_path, monkeypatch, capsys, result, status, state, exit_code,
):
    _assert_cli_summary(tmp_path, monkeypatch, capsys, result, status, state, exit_code)


@pytest.mark.parametrize(("history_state", "reason", "cancel_during_history", "status", "state", "exit_code"), (
    ("failed", "supplier_context_failed", False, "failed", "失败", 1),
    ("blocked", "disk_space_insufficient", False, "failed", "失败", 1),
    ("already_running", "history_maintenance_running", False, "pending", "等待", 1),
    ("cancelled", "cancelled", False, "cancelled", "取消", 130),
    ("completed", None, False, "completed", "完成", 0),
    ("already_current", None, False, "completed", "完成", 0),
    ("failed", "supplier_context_failed", True, "cancelled", "取消", 130),
))
def test_typed_history_terminal_state_reaches_the_cli_without_downgrading_failures(
    tmp_path, monkeypatch, capsys, history_state, reason, cancel_during_history, status, state, exit_code,
):
    cancelled = False
    history = replace(_history(state=history_state), reason=reason)

    def synchronize(_reference):
        nonlocal cancelled
        cancelled = cancel_during_history
        return history

    def forbidden(_reference):
        pytest.fail("history command must not start another task")

    result = SyncData(lambda: PreparedFixture(_NOW), synchronize, forbidden, forbidden, lambda: cancelled).execute("history")
    assert result.tasks[0].history is history and result.tasks[0].reason == reason
    assert result.tasks[0].status == status
    assert ProjectSyncResult(result)["status"] == status
    _assert_cli_summary(tmp_path, monkeypatch, capsys, result, status, state, exit_code)


@pytest.mark.parametrize(("partitions", "documents", "ocr", "unextractable", "failures", "reason"), (
    (0, 0, 0, 0, (), None),
    (0, 0, 0, 1, (), None),  # Terminal extraction limits remain data-quality unknowns.
    (2, 0, 0, 0, (), "announcements:pending"),
    (0, 3, 1, 0, (), "risk_documents:pending"),
    (2, 3, 1, 0, ("risk_documents:pending",), "risk_documents:pending,announcements:pending"),
    (None, None, None, None, (), "inventory:not_checked"),
    (0, 0, 0, 0, ("financial:timeout",), "financial:timeout"),
    (0, 3, 0, 0, ("risk_documents:company_raw_capacity_exhausted",),
     "risk_documents:company_raw_capacity_exhausted,risk_documents:pending"),
    (None, None, None, None, ("task:cancelled",), "task:cancelled"),
))
def test_company_terminal_reason_reaches_projection_and_cli(
    tmp_path, monkeypatch, capsys, partitions, documents, ocr, unextractable, failures, reason,
):
    company = CompanyEvidenceSyncResult(
        _NOW, 1, 1, 0, 0, 0, partitions, failures, 0,
        pending_documents=documents, ocr_documents=ocr, unextractable_documents=unextractable,
    )
    result = SyncData(
        lambda: PreparedFixture(_NOW), lambda _: _history(), lambda _: QfqUpdateResult(_NOW.date(), 1),
        lambda _: company, lambda: False,
    ).execute("company")
    cancelled = "task:cancelled" in failures
    status = "cancelled" if cancelled else "pending" if reason else "completed"
    assert result.tasks[0].status == status and result.tasks[0].reason == reason
    assert ProjectSyncResult(result)["tasks"][0]["reason"] == reason
    from tests.helpers.shared.configuration import RuntimeSettingsFixture
    from trader.entrypoints import cli

    runtime = RuntimeSettingsFixture()
    monkeypatch.setattr(cli, "LoadRuntimeSettings", lambda _: runtime)
    monkeypatch.setattr(cli, "_configuration_payload", lambda *_: {})
    monkeypatch.setattr("trader.bootstrap.PrepareRuntimeDisk", lambda settings, **_: settings)
    monkeypatch.setattr("trader.bootstrap.ExecuteSync", lambda *_, **__: result)
    messages = []
    progress = SyncProgress(messages.append, lambda: 0.0)
    args = cli.BuildParser().parse_args(["--config", str(tmp_path / "runtime.json"), "sync", "company"])
    assert cli._run_sync(args, progress) == (130 if cancelled else int(reason is not None))
    terminal = messages[-1]
    assert terminal.state == ("取消" if cancelled else "等待" if reason else "完成") and terminal.finished
    fields = " | ".join(field.text for field in terminal.fields)
    assert f"原因 {reason or '无'}" in fields
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == status and payload["tasks"][0]["reason"] == reason
    assert payload["tasks"][0]["pending_counts_checked"] is (partitions is not None)
    assert payload["tasks"][0]["unextractable_documents"] == unextractable


def test_company_incompatible_database_is_a_failure_with_explicit_material_scope(tmp_path, monkeypatch, capsys):
    def company(_):
        raise CompanyDatabaseIncompatibleError("company_evidence_database_contract_mismatch")

    result = SyncData(
        lambda: PreparedFixture(_NOW), lambda _: _history(), lambda _: QfqUpdateResult(_NOW.date(), 1),
        company, lambda: False,
    ).execute("company")
    assert result.tasks[0].reason == "CompanyDatabaseIncompatibleError"
    _assert_cli_summary(tmp_path, monkeypatch, capsys, result, "failed", "失败", 1,
                        diagnostic="company/raw/company_collection.sqlite3")


def _assert_cli_summary(tmp_path, monkeypatch, capsys, result, status, state, exit_code, diagnostic=None):
    from tests.helpers.shared.configuration import RuntimeSettingsFixture
    from trader.entrypoints import cli

    runtime = RuntimeSettingsFixture()
    monkeypatch.setattr(cli, "LoadRuntimeSettings", lambda _path: runtime)
    monkeypatch.setattr(cli, "_configuration_payload", lambda *_args: {})
    monkeypatch.setattr("trader.bootstrap.PrepareRuntimeDisk", lambda settings, **_kwargs: settings)
    monkeypatch.setattr("trader.bootstrap.ExecuteSync", lambda *_args, **_kwargs: result)
    messages = []
    progress = SyncProgress(messages.append, lambda: 0.0)
    args = cli.BuildParser().parse_args(["--config", str(tmp_path / "runtime.json"), "sync", "history"])

    assert cli._run_sync(args, progress) == exit_code
    if diagnostic is not None:
        assert any(diagnostic in field.text for message in messages for field in message.fields)
    terminal = messages[-1]
    assert terminal.state == state and terminal.finished
    fields = " | ".join(field.text for field in terminal.fields)
    assert ("前置失败" in fields) is (result.failure is not None)
    assert "前置失败 无" not in fields
    if status == "pending":
        assert f"history 待补齐 原因 {result.tasks[0].reason}" in fields
        assert "失败" not in fields
    payload = json.loads(capsys.readouterr().out)
    for task, projected in zip(result.tasks, payload["tasks"], strict=True):
        if task.history is not None:
            history = projected.pop("history")
            assert history["state"] == task.history.state and history["reason"] == task.history.reason
    assert payload == {
        "schema_version": "data_sync", "status": status, "failure": result.failure,
        "tasks": [{"task": task.task, "status": task.status, "reason": task.reason} for task in result.tasks],
    }


@pytest.mark.parametrize("target", ("all", "history", "market", "company"))
@pytest.mark.parametrize("error", (ValueError, sqlite3.OperationalError))
def test_common_preparation_failure_stops_all_tasks(target, error):
    def fail():
        raise error("invalid official population")

    def forbidden(value):
        pytest.fail("dependent task ran after preparation failed")

    result = SyncData(AdaptSyncDatabaseFailures(fail), forbidden, forbidden, forbidden, lambda: False).execute(target)
    assert result.tasks == () and result.exit_code == 1
    assert result.failure == f"preparation_{error.__name__}"


def test_incompatible_reference_database_keeps_specific_failure_category():
    def fail():
        raise ReferenceDatabaseIncompatibleError("reference_database_incompatible")

    def forbidden(value):
        pytest.fail("dependent task ran after preparation failed")

    result = SyncData(fail, forbidden, forbidden, forbidden, lambda: False).execute("history")
    assert result.failure == "preparation_ReferenceDatabaseIncompatibleError"
    assert result.tasks == () and result.exit_code == 1


@pytest.mark.parametrize("interrupt", (False, True))
def test_cancellation_stops_following_tasks(interrupt):
    def history(value):
        if interrupt:
            raise KeyboardInterrupt
        return _history(state="cancelled")

    def forbidden(value):
        pytest.fail("task ran after cancellation")

    result = SyncData(lambda: PreparedFixture(_NOW), history, forbidden, forbidden, lambda: False).execute("all")
    assert result.exit_code == 130 and result.cancelled


def test_cancel_before_preparation_has_no_side_effects():
    def forbidden(*args):
        pytest.fail("cancelled work ran")

    assert SyncData(forbidden, forbidden, forbidden, forbidden, lambda: True).execute("all").exit_code == 130


@pytest.mark.parametrize("during_preparation", (False, True))
@pytest.mark.parametrize("error", (InterruptedError, CancelledError, sqlite3.OperationalError))
def test_interrupted_io_is_cancelled_instead_of_failed(during_preparation, error):
    cancelled = False

    def interrupt(*_args):
        nonlocal cancelled
        cancelled = True
        raise error("cancelled")

    prepare = interrupt if during_preparation else lambda: PreparedFixture(_NOW)
    result = SyncData(
        AdaptSyncDatabaseFailures(prepare), interrupt, interrupt,
        AdaptSyncDatabaseFailures(interrupt), lambda: cancelled,
    ).execute("company")
    assert result.exit_code == 130 and result.failure is None
    assert not result.tasks if during_preparation else result.tasks[0].status == "cancelled"
    assert ProjectSyncResult(result)["status"] == "cancelled"
