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
from trader.download.domain.company_evidence import CompanyDatabaseIncompatibleError, CompanyDownloadStore
from trader.download.domain.company_sync import CompanySyncFamily
from tests.helpers.download.company_round import CompanySyncFamilyFixture, CompanySyncRoundFixture
from trader.download.domain.history_maintenance import HistoryMaintenanceStatus
from trader.download.entrypoints.sync_projection import ProjectSyncResult
from trader.download.infra.sync_database_failure import AdaptSyncDatabaseFailures
from trader.infra.sqlite_connection import SqliteSession

_NOW = datetime(2026, 10, 16, tzinfo=ZoneInfo("Asia/Shanghai"))


def _history(state="completed", unresolved_price_cells=0):
    return HistoryMaintenanceStatus(
        state, None, Path("history"), "tencent", None, "a" * 64, _NOW.date(), _NOW.date(), unresolved_price_cells
    )




@pytest.mark.parametrize("target", ("all", "history", "company"))
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

    def company(value):
        assert value is reference
        calls.append("company")
        return CompanySyncRoundFixture(_NOW)

    result = SyncData(prepare, history, company, lambda: False).execute(target)
    assert calls == ["prepare", *(("history", "company") if target == "all" else (target,))]
    assert result.exit_code == 0
    assert ProjectSyncResult(result)["status"] == "completed"


@pytest.mark.parametrize("pending", (False, True))
def test_failed_or_incomplete_history_continues_company(pending):
    calls = []

    def history(value):
        if pending:
            return replace(_history(unresolved_price_cells=1), reason="history_price_gaps_remaining")
        raise RuntimeError("history unavailable")

    def company(value):
        calls.append("company")
        return CompanySyncRoundFixture(_NOW)

    result = SyncData(lambda: PreparedFixture(_NOW), history, company, lambda: False).execute("all")
    assert calls == ["company"]
    assert result.exit_code == 1
    assert result.tasks[0].status == ("pending" if pending else "failed")
    assert result.tasks[0].reason == ("history_price_gaps_remaining" if pending else "RuntimeError")


@pytest.mark.parametrize("failed_task", ("history", "company"))
@pytest.mark.parametrize("error", (sqlite3.OperationalError, sqlite3.IntegrityError))
def test_sqlite_task_failure_preserves_results_and_continues_following_tasks(failed_task, error):
    calls = []
    failures = []
    history = _history()
    company = CompanySyncRoundFixture(_NOW)

    def synchronize(task, value):
        calls.append(task)
        if task == failed_task:
            raise error("private database detail")
        return value

    result = SyncData(
        lambda: PreparedFixture(_NOW),
        AdaptSyncDatabaseFailures(lambda _: synchronize("history", history)),
        AdaptSyncDatabaseFailures(lambda _: synchronize("company", company)),
        lambda: False,
        lambda context, exc: failures.append((context.stage, exc)),
    ).execute("all")

    assert calls == ["history", "company"]
    assert result.exit_code == 1 and result.failure is None and not result.cancelled
    assert [task.task for task in result.tasks] == calls
    for task in result.tasks:
        assert task.status == ("failed" if task.task == failed_task else "completed")
        assert task.reason == (error.__name__ if task.task == failed_task else None)
    if failed_task != "history":
        assert result.tasks[0].history is history
    if failed_task != "company":
        assert result.tasks[1].company is company
    assert len(failures) == 1
    stage, failure = failures[0]
    assert stage == f"{failed_task}_synchronization"
    assert isinstance(failure, SyncDatabaseError) and isinstance(failure.__cause__, error)
    projected = ProjectSyncResult(result)
    assert projected["status"] == "failed" and len(projected["tasks"]) == 2
    assert "private database detail" not in json.dumps(projected)


def test_company_round_exception_reports_its_safe_source_and_cause():
    def resume(_reference):
        try:
            raise OSError(5, "sensitive provider detail")
        except OSError as cause:
            raise ValueError("sensitive local path") from cause

    messages = []
    progress = SyncProgress(messages.append, lambda: 0.0)
    result = SyncData(
        lambda: PreparedFixture(_NOW), lambda _: _history(), resume, lambda: False, progress.report_failure,
    ).execute("company")

    assert result.tasks == (SyncTaskResult("company", "failed", "ValueError"),)
    assert len(messages) == 1 and messages[0].finished
    fields = " | ".join(field.text for field in messages[0].fields)
    assert "company_synchronization" in fields
    assert "ValueError:test_sync_data.py:resume:" in fields
    assert "OSError:errno_5:test_sync_data.py:resume:" in fields
    assert "sensitive" not in fields


def test_database_adapter_preserves_the_cause_from_the_shared_connection():
    def read_missing_table():
        with SqliteSession(":memory:") as connection:
            connection.run("SELECT * FROM missing_table")

    with pytest.raises(SyncDatabaseError) as failure:
        AdaptSyncDatabaseFailures(read_missing_table)()
    assert failure.value.category == "OperationalError"
    assert isinstance(failure.value.__cause__, sqlite3.OperationalError)
    assert "missing_table" not in str(failure.value)




def test_sqlite_failure_during_cancellation_keeps_previous_result():
    cancelled = False
    history = _history()

    def interrupt(_):
        nonlocal cancelled
        cancelled = True
        raise sqlite3.OperationalError("interrupted")

    result = SyncData(
        lambda: PreparedFixture(_NOW), lambda _: history,
        AdaptSyncDatabaseFailures(interrupt), lambda: cancelled,
    ).execute("all")
    assert result.cancelled and result.exit_code == 130 and result.failure is None
    assert [task.status for task in result.tasks] == ["completed", "cancelled"]
    assert result.tasks[0].history is history and result.tasks[1].task == "company"
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

    result = SyncData(lambda: PreparedFixture(_NOW), synchronize, forbidden, lambda: cancelled).execute("history")
    assert result.tasks[0].history is history and result.tasks[0].reason == reason
    assert result.tasks[0].status == status
    assert ProjectSyncResult(result)["status"] == status
    _assert_cli_summary(tmp_path, monkeypatch, capsys, result, status, state, exit_code)


@pytest.mark.parametrize(("pending", "gaps", "failure", "cancelled", "paused", "reason"), (
    (0, (), None, False, False, None),
    (0, ("source_admission_pending",), None, False, False, "fields:source_admission_pending"),
    (2, (), None, False, False, "fields:pending"),
    (None, (), None, False, False, "fields:inventory_not_checked"),
    (0, (), "timeout", False, False, "fields:timeout"),
    (3, (), "company_raw_capacity_exhausted", False, False, "fields:company_raw_capacity_exhausted"),
    (None, (), None, True, False, "task:cancelled"),
    (None, (), None, False, True, "company_round_budget_exhausted"),
    (None, ("supplier_input_rejected",), "company_round_page_number_mismatch", False, True, "company_round_budget_exhausted"),
))
def test_company_terminal_reason_reaches_projection_and_cli(
    tmp_path, monkeypatch, capsys, pending, gaps, failure, cancelled, paused, reason,
):
    company = CompanySyncRoundFixture(
        _NOW, families=tuple(CompanySyncFamilyFixture(
            family, pending_count=pending, gaps=gaps, failure=failure,
        ) if family is CompanySyncFamily.FIELDS else CompanySyncFamilyFixture(family) for family in CompanySyncFamily),
        cancelled=cancelled, budget_exhausted=paused,
    )
    result = SyncData(
        lambda: PreparedFixture(_NOW), lambda _: _history(),
        lambda _: company, lambda: False,
    ).execute("company")
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
    if failure is not None:
        assert f"company·fields 错误 {failure}" in fields
        assert f"公司失败记录 company/company_sync.sqlite3；轮次 {company.round_id}" in fields
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == status and payload["tasks"][0]["reason"] == reason
    assert payload["tasks"][0]["families"][-1]["pending_count"] == pending
    assert payload["tasks"][0]["families"][-1]["failure"] == failure
    assert payload["tasks"][0]["business_complete"] is (reason is None)
    assert payload["tasks"][0]["paused"] is (cancelled or paused)


@pytest.mark.parametrize("store,path", (
    (CompanyDownloadStore.COLLECTION, "company/raw/company_collection.sqlite3"),
    (CompanyDownloadStore.FACTS, "company/company_facts.sqlite3"),
    (CompanyDownloadStore.NORMALIZED, "company/company_normalized.sqlite3"),
    (CompanyDownloadStore.INPUTS, "company/company_inputs.sqlite3"),
    (CompanyDownloadStore.REQUEST_JOURNAL, "company/company_sync.sqlite3"),
))
def test_company_incompatible_database_is_a_failure_with_explicit_material_scope(tmp_path, monkeypatch, capsys, store, path):
    def company(_):
        raise CompanyDatabaseIncompatibleError(store)

    result = SyncData(
        lambda: PreparedFixture(_NOW), lambda _: _history(),
        company, lambda: False,
    ).execute("company")
    assert result.tasks[0].reason == "CompanyDatabaseIncompatibleError"
    assert ProjectSyncResult(result)["tasks"][0]["incompatible_company_store"] == store.value
    _assert_cli_summary(tmp_path, monkeypatch, capsys, result, "failed", "失败", 1,
                        diagnostic=path)


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
        if task.incompatible_company_store is not None:
            assert projected.pop("incompatible_company_store") == task.incompatible_company_store.value
    assert payload == {
         "status": status, "failure": result.failure,
        "tasks": [{"task": task.task, "status": task.status, "reason": task.reason} for task in result.tasks],
    }


@pytest.mark.parametrize("target", ("all", "history", "company"))
@pytest.mark.parametrize("error", (ValueError, sqlite3.OperationalError))
def test_common_preparation_failure_stops_all_tasks(target, error):
    def fail():
        raise error("invalid official population")

    def forbidden(value):
        pytest.fail("dependent task ran after preparation failed")

    result = SyncData(AdaptSyncDatabaseFailures(fail), forbidden, forbidden, lambda: False).execute(target)
    assert result.tasks == () and result.exit_code == 1
    assert result.failure == f"preparation_{error.__name__}"


def test_incompatible_reference_database_keeps_specific_failure_category():
    def fail():
        raise ReferenceDatabaseIncompatibleError("reference_database_incompatible")

    def forbidden(value):
        pytest.fail("dependent task ran after preparation failed")

    result = SyncData(fail, forbidden, forbidden, lambda: False).execute("history")
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

    result = SyncData(lambda: PreparedFixture(_NOW), history, forbidden, lambda: False).execute("all")
    assert result.exit_code == 130 and result.cancelled


def test_cancel_before_preparation_has_no_side_effects():
    def forbidden(*args):
        pytest.fail("cancelled work ran")

    assert SyncData(forbidden, forbidden, forbidden, lambda: True).execute("all").exit_code == 130


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
        AdaptSyncDatabaseFailures(prepare), interrupt,
        AdaptSyncDatabaseFailures(interrupt), lambda: cancelled,
    ).execute("company")
    assert result.exit_code == 130 and result.failure is None
    assert not result.tasks if during_preparation else result.tasks[0].status == "cancelled"
    assert ProjectSyncResult(result)["status"] == "cancelled"
