from pathlib import Path

import pytest

from trader.download.infra.history_automation_uninstall import (
    PlanHistoryAutomationRemoval,
    RemoveHistoryAutomation,
)


@pytest.mark.parametrize("platform", ("linux", "windows", "macos"))
def test_explicit_removal_only_removes_owned_files_and_is_idempotent(tmp_path: Path, platform: str) -> None:
    plan = PlanHistoryAutomationRemoval(platform, tmp_path, 1000)
    calls = []
    for path in (*plan.paths, plan.receipt):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Managed by Trader history automation\n", encoding="utf-8")
    assert RemoveHistoryAutomation(plan, run=calls.append) == (*plan.paths, plan.receipt)
    assert tuple(calls) == plan.commands
    assert not any(path.exists() for path in (*plan.paths, plan.receipt))
    assert RemoveHistoryAutomation(plan, run=calls.append) == ()
    assert tuple(calls) == plan.commands


@pytest.mark.parametrize("invalid", ("foreign", "symlink", "missing_receipt"))
def test_unverified_ownership_never_calls_scheduler_or_deletes_files(tmp_path, invalid):
    plan = PlanHistoryAutomationRemoval("linux", tmp_path, 1000)
    for path in (*plan.paths, plan.receipt):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Managed by Trader history automation\n")
    if invalid == "foreign":
        plan.paths[0].write_text("foreign service\n")
    elif invalid == "symlink":
        plan.paths[0].unlink()
        plan.paths[0].symlink_to(plan.receipt)
    else:
        plan.receipt.unlink()
    calls = []
    with pytest.raises(ValueError, match="ownership|receipt"):
        RemoveHistoryAutomation(plan, run=calls.append)
    assert calls == []
    assert all(path.exists() for path in plan.paths)


def test_failed_scheduler_command_retains_removal_receipt_and_tasks(tmp_path):
    plan = PlanHistoryAutomationRemoval("linux", tmp_path, 1000)
    for path in (*plan.paths, plan.receipt):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Managed by Trader history automation\n")

    def fail(command):
        raise RuntimeError("scheduler unavailable")

    with pytest.raises(RuntimeError):
        RemoveHistoryAutomation(plan, run=fail)
    assert all(path.exists() for path in (*plan.paths, plan.receipt))
