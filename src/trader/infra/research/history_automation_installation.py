"""Render and manage reversible, user-level history scheduler tasks."""

from __future__ import annotations

import html
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Literal

HistoryAutomationPlatform = Literal["linux", "windows", "macos"]
InstallationState = Literal["installed", "removed", "cancelled"]
CommandRunner = Callable[[tuple[str, ...]], None]
DependencyChecker = Callable[["HistoryAutomationInstallationRequest"], None]


@dataclass(frozen=True)
class HistoryAutomationInstallationRequest:
    platform: HistoryAutomationPlatform
    project_root: Path
    config_path: Path
    python_executable: Path
    user_home: Path
    user_id: int

    def __post_init__(self) -> None:
        if (
            self.platform not in {"linux", "windows", "macos"}
            or self.user_id < 0
            or any(
                not path.is_absolute()
                for path in (self.project_root, self.config_path, self.python_executable, self.user_home)
            )
        ):
            raise ValueError("history automation installation request is invalid")
        if any(any(character in str(path) for character in ("\x00", "\r", "\n")) for path in self.paths):
            raise ValueError("history automation installation path contains a control character")

    @property
    def paths(self) -> tuple[Path, Path, Path, Path]:
        return self.project_root, self.config_path, self.python_executable, self.user_home


@dataclass(frozen=True)
class HistoryAutomationManagedFile:
    path: Path
    content: str

    def __post_init__(self) -> None:
        if not self.path.is_absolute() or "Managed by Trader history automation" not in self.content:
            raise ValueError("history automation managed file is invalid")


@dataclass(frozen=True)
class HistoryAutomationInstallationPlan:
    request: HistoryAutomationInstallationRequest
    files: tuple[HistoryAutomationManagedFile, ...]
    receipt: HistoryAutomationManagedFile
    install_commands: tuple[tuple[str, ...], ...]
    uninstall_commands: tuple[tuple[str, ...], ...]

    def __post_init__(self) -> None:
        if not self.files or not self.install_commands or not self.uninstall_commands:
            raise ValueError("history automation installation plan is empty")


@dataclass(frozen=True)
class HistoryAutomationInstallationResult:
    state: InstallationState
    platform: HistoryAutomationPlatform
    managed_paths: tuple[Path, ...]


def plan_history_automation_installation(
    request: HistoryAutomationInstallationRequest,
) -> HistoryAutomationInstallationPlan:
    files: tuple[HistoryAutomationManagedFile, ...]
    install: tuple[tuple[str, ...], ...]
    uninstall: tuple[tuple[str, ...], ...]
    if request.platform == "linux":
        unit_root = request.user_home / ".config" / "systemd" / "user"
        service = _render(
            "linux.service.in",
            {
                "WORKING_DIRECTORY": _systemd_path(request.project_root),
                "PYTHON_EXECUTABLE": _systemd_quote(request.python_executable),
                "CONFIG_PATH": _systemd_quote(request.config_path),
            },
        )
        timer = _template("linux.timer")
        files = (
            HistoryAutomationManagedFile(unit_root / "trader-history-maintenance.service", service),
            HistoryAutomationManagedFile(unit_root / "trader-history-maintenance.timer", timer),
        )
        receipt_path = request.user_home / ".local" / "state" / "trader" / "history-automation.installed"
        install = (
            ("systemctl", "--user", "daemon-reload"),
            ("systemctl", "--user", "enable", "--now", "trader-history-maintenance.timer"),
        )
        uninstall = (
            ("systemctl", "--user", "disable", "--now", "trader-history-maintenance.timer"),
            ("systemctl", "--user", "daemon-reload"),
        )
    elif request.platform == "windows":
        target = request.user_home / "AppData" / "Local" / "Trader" / "history-automation.xml"
        task = _render(
            "windows.xml.in",
            {
                "WORKING_DIRECTORY": html.escape(str(request.project_root), quote=True),
                "PYTHON_EXECUTABLE": html.escape(str(request.python_executable), quote=True),
                "CONFIG_PATH_ARGUMENT": html.escape(_windows_argument(request.config_path), quote=True),
            },
        )
        files = (HistoryAutomationManagedFile(target, task),)
        receipt_path = request.user_home / "AppData" / "Local" / "Trader" / "history-automation.installed"
        install = (("schtasks.exe", "/Create", "/TN", r"Trader\HistoryMaintenance", "/XML", str(target), "/F"),)
        uninstall = (("schtasks.exe", "/Delete", "/TN", r"Trader\HistoryMaintenance", "/F"),)
    else:
        target = request.user_home / "Library" / "LaunchAgents" / "com.local.trader.history-maintenance.plist"
        task = _render(
            "macos.plist.in",
            {
                "WORKING_DIRECTORY": html.escape(str(request.project_root), quote=True),
                "PYTHON_EXECUTABLE": html.escape(str(request.python_executable), quote=True),
                "CONFIG_PATH": html.escape(str(request.config_path), quote=True),
            },
        )
        files = (HistoryAutomationManagedFile(target, task),)
        receipt_path = request.user_home / "Library" / "Application Support" / "Trader" / "history-automation.installed"
        service = f"gui/{request.user_id}/com.local.trader.history-maintenance"
        install = (("launchctl", "bootstrap", f"gui/{request.user_id}", str(target)),)
        uninstall = (("launchctl", "bootout", service),)
    receipt = HistoryAutomationManagedFile(
        receipt_path,
        f"Managed by Trader history automation.\nplatform={request.platform}\n",
    )
    return HistoryAutomationInstallationPlan(request, files, receipt, install, uninstall)


def apply_history_automation_installation(
    plan: HistoryAutomationInstallationPlan,
    *,
    confirmed: bool,
    command_runner: CommandRunner = lambda command: _run_command(command),
    dependency_checker: DependencyChecker = lambda request: _check_dependencies(request),
) -> HistoryAutomationInstallationResult:
    if not confirmed:
        return _result("cancelled", plan)
    dependency_checker(plan.request)
    _require_owned_or_missing((*plan.files, plan.receipt))
    if plan.receipt.path.is_file() and all(managed.path.is_file() for managed in plan.files):
        return _result("installed", plan)
    for managed in plan.files:
        _atomic_write(managed.path, managed.content)
    for command in plan.install_commands:
        command_runner(command)
    _atomic_write(plan.receipt.path, plan.receipt.content)
    return _result("installed", plan)


def remove_history_automation_installation(
    plan: HistoryAutomationInstallationPlan,
    *,
    confirmed: bool,
    command_runner: CommandRunner = lambda command: _run_command(command),
) -> HistoryAutomationInstallationResult:
    if not confirmed:
        return _result("cancelled", plan)
    _require_owned_or_missing((*plan.files, plan.receipt))
    files_present = any(managed.path.exists() for managed in plan.files)
    if not files_present and not plan.receipt.path.exists():
        return _result("removed", plan)
    if files_present:
        command_runner(plan.uninstall_commands[0])
    for managed in plan.files:
        managed.path.unlink(missing_ok=True)
    for command in plan.uninstall_commands[1:]:
        command_runner(command)
    plan.receipt.path.unlink(missing_ok=True)
    return _result("removed", plan)


def _check_dependencies(request: HistoryAutomationInstallationRequest) -> None:
    if (
        not request.project_root.is_dir()
        or not request.config_path.is_file()
        or not request.python_executable.is_file()
    ):
        raise RuntimeError("history automation requires an existing project, config, and virtual-environment Python")
    if request.platform in {"windows", "macos"} and datetime.now().astimezone().utcoffset() != timedelta(hours=8):
        raise RuntimeError("Windows and macOS history automation require the system timezone to be Asia/Shanghai")
    scheduler = {"linux": "systemctl", "windows": "schtasks.exe", "macos": "launchctl"}[request.platform]
    if shutil.which(scheduler) is None:
        raise RuntimeError("history automation user scheduler is unavailable")
    try:
        completed = subprocess.run(
            (
                str(request.python_executable),
                "-c",
                "import baostock,sys,trader; from trader.entrypoints.cli import build_parser; "
                "build_parser(); raise SystemExit(sys.prefix == sys.base_prefix)",
            ),
            cwd=request.project_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=30.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("history automation dependency validation failed") from exc
    if completed.returncode != 0:
        raise RuntimeError("history automation dependencies are unavailable in the selected virtual environment")


def _run_command(command: tuple[str, ...]) -> None:
    try:
        subprocess.run(command, check=True, timeout=30.0)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("history automation scheduler command failed") from exc


def _require_owned_or_missing(files: tuple[HistoryAutomationManagedFile, ...]) -> None:
    for managed in files:
        if managed.path.exists() and managed.path.read_text(encoding="utf-8") != managed.content:
            raise RuntimeError(f"history automation task is not owned by Trader: {managed.path}")


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _result(state: InstallationState, plan: HistoryAutomationInstallationPlan) -> HistoryAutomationInstallationResult:
    return HistoryAutomationInstallationResult(
        state,
        plan.request.platform,
        (*tuple(item.path for item in plan.files), plan.receipt.path),
    )


def _template(name: str) -> str:
    return (
        resources.files("trader.infra.research.history_automation_templates").joinpath(name).read_text(encoding="utf-8")
    )


def _render(name: str, replacements: dict[str, str]) -> str:
    content = _template(name)
    for key, value in replacements.items():
        content = content.replace("{{" + key + "}}", value)
    if "{{" in content or "}}" in content:
        raise RuntimeError("history automation template contains an unresolved placeholder")
    return content


def _systemd_quote(path: Path) -> str:
    return '"' + str(path).replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"') + '"'


def _systemd_path(path: Path) -> str:
    return str(path).replace("%", "%%").replace("\\", r"\x5c").replace(" ", r"\x20").replace('"', r"\x22")


def _windows_argument(path: Path) -> str:
    return '"' + str(path).replace('"', '\\"') + '"'


__all__ = [
    "HistoryAutomationInstallationPlan",
    "HistoryAutomationInstallationRequest",
    "HistoryAutomationInstallationResult",
    "apply_history_automation_installation",
    "plan_history_automation_installation",
    "remove_history_automation_installation",
]
