from concurrent.futures import CancelledError
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tests.helpers.shared.reference_fixtures import PreparedFixture
from trader.download.application.prepare_reference import ReferenceDatabaseIncompatibleError
from trader.download.application.sync_data import SyncData, SyncResult, SyncTaskResult
from trader.download.application.sync_progress import SyncProgress
from trader.download.domain.company_evidence import CompanyEvidenceSyncResult
from trader.download.domain.history_maintenance import HistoryMaintenanceStatus
from trader.download.domain.qfq_window import QfqUpdateResult
from trader.download.entrypoints.sync_projection import ProjectSyncResult

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


def _assert_cli_summary(tmp_path, monkeypatch, capsys, result, status, state, exit_code):
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
def test_common_preparation_failure_stops_all_tasks(target):
    def fail():
        raise ValueError("invalid official population")

    def forbidden(value):
        pytest.fail("dependent task ran after preparation failed")

    result = SyncData(fail, forbidden, forbidden, forbidden, lambda: False).execute(target)
    assert result.tasks == () and result.exit_code == 1


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
@pytest.mark.parametrize("error", (InterruptedError, CancelledError))
def test_interrupted_io_is_cancelled_instead_of_failed(during_preparation, error):
    cancelled = False

    def interrupt(*_args):
        nonlocal cancelled
        cancelled = True
        raise error("cancelled")

    prepare = interrupt if during_preparation else lambda: PreparedFixture(_NOW)
    result = SyncData(prepare, interrupt, interrupt, interrupt, lambda: cancelled).execute("company")
    assert result.exit_code == 130 and result.failure is None
    assert not result.tasks if during_preparation else result.tasks[0].status == "cancelled"
    assert ProjectSyncResult(result)["status"] == "cancelled"
