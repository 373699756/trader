import json

import pytest

from trader.entrypoints.cli import BuildParser


@pytest.mark.parametrize("command", ("history-automation-install", "history-maintenance-run"))
def test_retired_automation_commands_are_rejected(command):
    with pytest.raises(SystemExit) as error:
        BuildParser().parse_args([command])
    assert error.value.code == 2


def test_manual_history_and_explicit_uninstall_are_available():
    assert BuildParser().parse_args(["sync", "history"]).target == "history"
    assert BuildParser().parse_args(["history-automation-uninstall"]).command == "history-automation-uninstall"


def test_explicit_uninstall_dispatches_once_and_projects_failure(monkeypatch, capsys):
    from pathlib import Path

    from trader.download.entrypoints.commands import RunDownloadCommand

    def fail():
        raise RuntimeError("scheduler unavailable")

    monkeypatch.setattr("trader.bootstrap.ExecuteHistoryAutomationRemoval", fail)
    assert (
        RunDownloadCommand(
            "history-automation-uninstall", config_path=Path(__file__).parents[2] / "config/runtime.json"
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out)["status"] == "failed"
