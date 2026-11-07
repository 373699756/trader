"""Resolve configured data locations and render actionable disk diagnostics."""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TextIO

from trader.infra.settings import LoadRuntimeSettings
from trader.infra.settings.models import RuntimeSettings
from trader.infra.settings.disk_medium import SaveDiskMedium
from trader.infra.disk_probe import DiskProbe
from trader.infra.disk_types import DiskLocation, DiskMedium

_REASONS = {
    "path_unavailable": "目录或其父目录不可访问",
    "platform_unsupported": "当前系统未提供受支持的硬盘检测接口",
    "probe_timeout": "硬盘元数据检测超过3秒",
    "permission_denied": "没有读取设备元数据的权限",
    "device_metadata_unavailable": "无法取得可靠的物理介质信息（虚拟盘、网络盘或设备信息缺失）",
}


def InspectDataDisk(
    settings: RuntimeSettings,
    probe: DiskProbe,
    *,
    additional_paths: Sequence[Path] = (),
) -> tuple[DiskLocation, ...]:
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
    result = [DiskLocation(role, path, probe.inspect(path)) for role, path in locations]
    external_probe = DiskProbe()
    result.extend(
        DiskLocation(f"external.{index}", path, external_probe.inspect(path))
        for index, path in enumerate(additional_paths)
    )
    return tuple(result)


def ConfigureDisk(
    settings: RuntimeSettings,
    *,
    additional_paths: Sequence[Path] = (),
    choose: bool = False,
    report: bool = True,
    report_line: Callable[[str], None] | None = None,
) -> RuntimeSettings:
    """Resolve terminal choices before runtime resources use the disk profile."""
    locations = InspectDataDisk(
        settings, DiskProbe(settings.disk_medium), additional_paths=additional_paths
    )
    if choose and settings.disk_medium is None:
        selected = PromptUnknownDisk(tuple(item for item in locations if not item.role.startswith("external.")), report_line=report_line)
        if selected is not None:
            SaveDiskMedium(settings.config_path, settings.disk_medium, selected)
            _report_disk("[存储选择] 已保存到运行配置；下次启动将复用此选择。", report_line)
            settings = LoadRuntimeSettings(settings.config_path)
            locations = InspectDataDisk(
                settings, DiskProbe(settings.disk_medium), additional_paths=additional_paths
            )
    if report:
        ReportDiskProfiles(settings, locations, report_line=report_line)
    return settings


def ReportDiskProfiles(
    settings: RuntimeSettings, locations: tuple[DiskLocation, ...], *, report_line: Callable[[str], None] | None = None,
) -> None:
    """Summarize known media and keep unknown warnings per device on stderr."""
    groups: dict[tuple[str, ...], list[DiskLocation]] = {}
    for item in locations:
        key: tuple[str, ...] = (item.profile.kind, item.profile.source)
        if item.profile.kind == "unknown":
            key += (item.profile.device, "external" if item.role.startswith("external.") else "app")
        groups.setdefault(key, []).append(item)
    for items in groups.values():
        profile = items[0].profile
        source = "已使用手动配置" if profile.source == "configuration" else "自动检测"
        _report_disk(f"[存储检测] {profile.kind.upper()}（{source}）", report_line)
        if profile.kind != "unknown":
            continue
        names = ", ".join(DisplayDiskPath(item.path, settings.project_root) for item in items)
        if items[0].role.startswith("external."):
            _report_disk(f"[存储检测] 外部目录介质仍未知：{names}；请核实该目录所在设备。", report_line)
            continue
        _report_disk(
            "[存储配置需修改] "
            f"原因：{_REASONS.get(profile.reason, profile.reason)}。unknown按保守策略处理。\n"
            f"受影响目录：{names}\n"
            f"请修改 {DisplayDiskPath(settings.config_path, settings.project_root)} 的 disk_medium，"
            '例如："disk_medium": "ssd" 或 "disk_medium": "hdd"。\n'
            "此选项统一应用于程序的数据路径，保存后重新启动命令生效。",
            report_line,
        )


def PromptUnknownDisk(
    locations: tuple[DiskLocation, ...],
    *,
    input_stream: TextIO | None = None,
    report_line: Callable[[str], None] | None = None,
) -> DiskMedium | None:
    """Ask once for the global medium when automatic detection is inconclusive."""
    source = input_stream if input_stream is not None else sys.stdin
    if not source.isatty() or not any(item.profile.kind == "unknown" for item in locations):
        return None
    _report_disk("[存储选择] 无法自动确认介质，请为程序选择统一的存储介质。", report_line)
    while True:
        _report_disk("请选择 1=SSD 固态硬盘、2=HDD 机械硬盘；回车暂不设置：", report_line)
        answer = source.readline()
        if not answer or not answer.strip():
            return None
        selected = answer.strip().lower()
        if selected in {"1", "ssd"}:
            return "ssd"
        if selected in {"2", "hdd"}:
            return "hdd"
        _report_disk("请输入 1、2 或直接回车。", report_line)


def _report_disk(text: str, report_line: Callable[[str], None] | None) -> None:
    for line in text.splitlines():
        if report_line is None:
            print(line, file=sys.stderr, flush=True)
        else:
            report_line(line)


def DisplayDiskPath(path: Path, project_root: Path) -> str:
    return path.relative_to(project_root).as_posix() if path.is_relative_to(project_root) else str(path)
