import pytest

from trader.entrypoints.cli import BuildParser


@pytest.mark.parametrize("command", ("history-automation-install", "history-automation-uninstall", "history-maintenance-run"))
def test_retired_automation_commands_are_rejected(command):
    with pytest.raises(SystemExit) as error:
        BuildParser().parse_args([command])
    assert error.value.code == 2


def test_manual_history_and_reminder_status_are_available():
    assert BuildParser().parse_args(["sync", "history"]).target == "history"
    assert BuildParser().parse_args(["history-automation-status"]).command == "history-automation-status"
