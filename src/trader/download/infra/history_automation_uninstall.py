"""Explicit removal of retired Trader user scheduler tasks; no installation path."""

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

HistoryAutomationPlatform = Literal["linux", "windows", "macos"]
_MARKER = "Managed by Trader history automation"


@dataclass(frozen=True)
class HistoryAutomationRemoval:
    platform: HistoryAutomationPlatform
    paths: tuple[Path, ...]
    receipt: Path
    commands: tuple[tuple[str, ...], ...]


def PlanHistoryAutomationRemoval(
    platform: HistoryAutomationPlatform, user_home: Path, user_id: int
) -> HistoryAutomationRemoval:
    if not user_home.is_absolute() or user_id < 0:
        raise ValueError("history automation removal requires an absolute home and valid user")
    if platform == "linux":
        root = user_home / ".config/systemd/user"
        return HistoryAutomationRemoval(
            platform,
            (root / "trader-history-maintenance.service", root / "trader-history-maintenance.timer"),
            user_home / ".local/state/trader/history-automation.installed",
            (
                ("systemctl", "--user", "disable", "--now", "trader-history-maintenance.timer"),
                ("systemctl", "--user", "stop", "trader-history-maintenance.service"),
                ("systemctl", "--user", "daemon-reload"),
            ),
        )
    if platform == "windows":
        root = user_home / "AppData/Local/Trader"
        return HistoryAutomationRemoval(
            platform,
            (root / "history-automation.xml",),
            root / "history-automation.installed",
            (("schtasks.exe", "/Delete", "/TN", r"Trader\HistoryMaintenance", "/F"),),
        )
    if platform == "macos":
        return HistoryAutomationRemoval(
            platform,
            (user_home / "Library/LaunchAgents/com.local.trader.history-maintenance.plist",),
            user_home / "Library/Application Support/Trader/history-automation.installed",
            (("launchctl", "bootout", f"gui/{user_id}/com.local.trader.history-maintenance"),),
        )
    raise ValueError("history automation platform is unsupported")


def RemoveHistoryAutomation(
    plan: HistoryAutomationRemoval, *, run: Callable[[tuple[str, ...]], None]
) -> tuple[Path, ...]:
    paths = (*plan.paths, plan.receipt)
    for path in paths:
        if path.is_symlink() or (
            path.exists()
            and (
                not path.is_file() or path.stat().st_size > 64 * 1024 or _MARKER not in path.read_text(encoding="utf-8")
            )
        ):
            raise ValueError("history_automation_ownership_unverified")
    if not any(path.exists() for path in paths):
        return ()
    if not plan.receipt.is_file():
        raise ValueError("history_automation_receipt_missing")
    for command in plan.commands:
        if command[-1] != "daemon-reload":
            run(command)
    removed = tuple(path for path in paths if path.exists())
    for path in plan.paths:
        path.unlink(missing_ok=True)
    for command in plan.commands:
        if command[-1] == "daemon-reload":
            run(command)
    plan.receipt.unlink(missing_ok=True)
    return removed


def RunHistoryRemovalCommand(command: tuple[str, ...]) -> None:
    try:
        subprocess.run(command, check=True, timeout=30.0, capture_output=True)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("history_automation_removal_failed") from exc


__all__ = [
    "HistoryAutomationPlatform",
    "HistoryAutomationRemoval",
    "PlanHistoryAutomationRemoval",
    "RemoveHistoryAutomation",
    "RunHistoryRemovalCommand",
]
