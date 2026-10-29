"""Resolve configured data locations and render actionable storage diagnostics."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from trader.infra.settings import LoadRuntimeSettings
from trader.infra.settings.models import RuntimeSettings
from trader.infra.settings.storage import SaveStorageMedium
from trader.infra.storage_probe import StorageProbe
from trader.infra.storage_types import StorageLocation, StorageMedium

_REASONS = {
    "path_unavailable": "目录或其父目录不可访问",
    "platform_unsupported": "当前系统未提供受支持的硬盘检测接口",
    "probe_timeout": "硬盘元数据检测超过3秒",
    "permission_denied": "没有读取设备元数据的权限",
    "device_metadata_unavailable": "无法取得可靠的物理介质信息（虚拟盘、网络盘或设备信息缺失）",
}


def InspectDataStorage(
    settings: RuntimeSettings,
    probe: StorageProbe,
    *,
    additional_paths: Sequence[Path] = (),
) -> tuple[StorageLocation, ...]:
    paths = settings.data_paths
    locations = [
        ("runtime", settings.runtime_dir),
        ("history", paths.history),
        ("daily_window", paths.daily_window),
        ("reference", paths.reference),
        ("company", paths.company),
        ("company_collection", paths.company_collection),
    ]
    for profile in ("v1", "v2", "v3", "v5"):
        locations.extend(
            (
                (f"models.{profile}", paths.models / profile),
                (f"samples.{profile}", paths.samples / profile),
                (f"recommendation.{profile}", paths.recommendation(profile)),
                (f"research.{profile}", paths.research(profile)),
            )
        )
    result = [StorageLocation(role, path, probe.inspect(path)) for role, path in locations]
    external_probe = StorageProbe()
    result.extend(
        StorageLocation(f"external.{index}", path, external_probe.inspect(path))
        for index, path in enumerate(additional_paths)
    )
    return tuple(result)


def ConfigureStorage(
    settings: RuntimeSettings,
    *,
    additional_paths: Sequence[Path] = (),
    choose: bool = False,
    report: bool = True,
) -> RuntimeSettings:
    """Resolve terminal choices before runtime resources use the storage profile."""
    locations = InspectDataStorage(
        settings, StorageProbe(settings.storage_medium), additional_paths=additional_paths
    )
    if choose and settings.storage_medium is None:
        selected = PromptUnknownStorage(tuple(item for item in locations if not item.role.startswith("external.")))
        if selected is not None:
            SaveStorageMedium(settings.config_path, settings.storage_medium, selected)
            print("[存储选择] 已保存到运行配置；下次启动将复用此选择。", file=sys.stderr)
            settings = LoadRuntimeSettings(settings.config_path)
            locations = InspectDataStorage(
                settings, StorageProbe(settings.storage_medium), additional_paths=additional_paths
            )
    if report:
        ReportStorageProfiles(settings, locations)
    return settings


def ReportStorageProfiles(settings: RuntimeSettings, locations: tuple[StorageLocation, ...]) -> None:
    """Summarize known media and keep unknown warnings per device on stderr."""
    groups: dict[tuple[str, ...], list[StorageLocation]] = {}
    for item in locations:
        key: tuple[str, ...] = (item.profile.kind, item.profile.source)
        if item.profile.kind == "unknown":
            key += (item.profile.device, "external" if item.role.startswith("external.") else "app")
        groups.setdefault(key, []).append(item)
    for items in groups.values():
        profile = items[0].profile
        source = "已使用手动配置" if profile.source == "configuration" else "自动检测"
        print(f"[存储检测] {profile.kind.upper()}（{source}）", file=sys.stderr)
        if profile.kind != "unknown":
            continue
        names = ", ".join(DisplayStoragePath(item.path, settings.project_root) for item in items)
        if items[0].role.startswith("external."):
            print(f"[存储检测] 外部目录介质仍未知：{names}；请核实该目录所在设备。", file=sys.stderr)
            continue
        print(
            "[存储配置需修改] "
            f"原因：{_REASONS.get(profile.reason, profile.reason)}。unknown按保守策略处理。\n"
            f"受影响目录：{names}\n"
            f"请修改 {DisplayStoragePath(settings.config_path, settings.project_root)} 的 storage，"
            '例如："storage": "ssd" 或 "storage": "hdd"。\n'
            "此选项统一应用于程序的数据路径，保存后重新启动命令生效。",
            file=sys.stderr,
        )


def PromptUnknownStorage(
    locations: tuple[StorageLocation, ...],
    *,
    input_stream: TextIO | None = None,
) -> StorageMedium | None:
    """Ask once for the global medium when automatic detection is inconclusive."""
    source = input_stream if input_stream is not None else sys.stdin
    if not source.isatty() or not any(item.profile.kind == "unknown" for item in locations):
        return None
    print("[存储选择] 无法自动确认介质，请为程序选择统一的存储介质。", file=sys.stderr)
    while True:
        print("请选择 1=SSD 固态硬盘、2=HDD 机械硬盘；回车暂不设置：", end="", file=sys.stderr, flush=True)
        answer = source.readline()
        if not answer or not answer.strip():
            return None
        selected = answer.strip().lower()
        if selected in {"1", "ssd"}:
            return "ssd"
        if selected in {"2", "hdd"}:
            return "hdd"
        print("请输入 1、2 或直接回车。", file=sys.stderr)


def DisplayStoragePath(path: Path, project_root: Path) -> str:
    return path.relative_to(project_root).as_posix() if path.is_relative_to(project_root) else str(path)
