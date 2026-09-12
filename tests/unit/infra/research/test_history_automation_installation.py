from __future__ import annotations

import plistlib
import xml.etree.ElementTree as element_tree
from pathlib import Path

import pytest

from trader.infra.research.history_automation_installation import (
    HistoryAutomationInstallationRequest,
    apply_history_automation_installation,
    plan_history_automation_installation,
    remove_history_automation_installation,
)


@pytest.mark.parametrize("platform", ("linux", "windows", "macos"))
def test_platform_plans_use_one_fixed_zero_argument_command_and_never_train(
    platform: str,
    tmp_path: Path,
) -> None:
    root = tmp_path / "Trader Project"
    config = root / "config" / "runtime.json"
    python = root / ".venv" / ("Scripts/python.exe" if platform == "windows" else "bin/python")
    request = HistoryAutomationInstallationRequest(platform, root, config, python, tmp_path / "user", 1000)

    plan = plan_history_automation_installation(request)
    rendered = "\n".join(item.content for item in plan.files)

    assert "scheduled-history-maintenance" in rendered
    assert "train-tomorrow" not in rendered
    assert "pip install" not in rendered
    assert str(root) in rendered
    assert str(config) in rendered
    assert str(python) in rendered
    assert "DEEPSEEK_API_KEY" not in rendered
    assert "TUSHARE_TOKEN" not in rendered
    assert all("sudo" not in argument.lower() for command in plan.install_commands for argument in command)
    if platform == "linux":
        escaped_root = str(root).replace(" ", r"\x20")
        assert f"WorkingDirectory={escaped_root}" in rendered
        assert f'WorkingDirectory="{root}"' not in rendered
        assert "15:10:00 Asia/Shanghai" in rendered
        assert "20:30:00 Asia/Shanghai" in rendered
        assert "Persistent=true" in rendered
        assert "TimeoutStartSec=12h" in rendered
    elif platform == "windows":
        assert "StartWhenAvailable>true" in rendered
        assert "15:10:00" in rendered
        assert "20:30:00" in rendered
        assert "MultipleInstancesPolicy>Parallel" in rendered
    else:
        assert "StartCalendarInterval" in rendered
        assert "RunAtLoad" in rendered


def test_install_is_confirmation_gated_reversible_and_refuses_foreign_files(tmp_path: Path) -> None:
    root = tmp_path / "project"
    config = root / "config" / "runtime.json"
    python = root / ".venv" / "bin" / "python"
    config.parent.mkdir(parents=True)
    config.write_text("{}", encoding="utf-8")
    python.parent.mkdir(parents=True)
    python.write_text("python", encoding="utf-8")
    request = HistoryAutomationInstallationRequest("linux", root, config, python, tmp_path / "user", 1000)
    plan = plan_history_automation_installation(request)
    commands: list[tuple[str, ...]] = []

    cancelled = apply_history_automation_installation(
        plan,
        confirmed=False,
        command_runner=commands.append,
        dependency_checker=lambda _request: None,
    )
    assert cancelled.state == "cancelled"
    assert commands == []
    assert all(not item.path.exists() for item in plan.files)

    installed = apply_history_automation_installation(
        plan,
        confirmed=True,
        command_runner=commands.append,
        dependency_checker=lambda _request: None,
    )
    assert installed.state == "installed"
    assert all(item.path.read_text(encoding="utf-8") == item.content for item in plan.files)
    assert plan.receipt.path.read_text(encoding="utf-8") == plan.receipt.content
    assert commands == list(plan.install_commands)

    replayed = apply_history_automation_installation(
        plan,
        confirmed=True,
        command_runner=commands.append,
        dependency_checker=lambda _request: None,
    )
    assert replayed.state == "installed"
    assert commands == list(plan.install_commands)

    removed = remove_history_automation_installation(plan, confirmed=True, command_runner=commands.append)
    assert removed.state == "removed"
    assert all(not item.path.exists() for item in plan.files)
    assert not plan.receipt.path.exists()
    assert commands[-len(plan.uninstall_commands) :] == list(plan.uninstall_commands)

    repeated_removal = remove_history_automation_installation(
        plan,
        confirmed=True,
        command_runner=commands.append,
    )
    assert repeated_removal.state == "removed"
    assert commands[-len(plan.uninstall_commands) :] == list(plan.uninstall_commands)

    foreign = plan.files[0]
    foreign.path.parent.mkdir(parents=True, exist_ok=True)
    foreign.path.write_text("foreign task", encoding="utf-8")
    with pytest.raises(RuntimeError, match="not owned"):
        apply_history_automation_installation(
            plan,
            confirmed=True,
            command_runner=commands.append,
            dependency_checker=lambda _request: None,
        )


def test_windows_and_macos_templates_are_parseable_native_documents(tmp_path: Path) -> None:
    root = tmp_path / "project"
    config = root / "config" / "runtime.json"
    user_home = tmp_path / "user"
    windows = plan_history_automation_installation(
        HistoryAutomationInstallationRequest(
            "windows",
            root,
            config,
            root / ".venv" / "Scripts" / "python.exe",
            user_home,
            1000,
        )
    )
    macos = plan_history_automation_installation(
        HistoryAutomationInstallationRequest(
            "macos",
            root,
            config,
            root / ".venv" / "bin" / "python",
            user_home,
            1000,
        )
    )

    assert element_tree.fromstring(windows.files[0].content).tag.endswith("Task")
    assert plistlib.loads(macos.files[0].content.encode("utf-8"))["RunAtLoad"] is True
