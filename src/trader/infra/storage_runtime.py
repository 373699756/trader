"""Resolve configured data locations and render actionable storage diagnostics."""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Literal, TextIO

from trader.infra.settings import LoadRuntimeSettings
from trader.infra.settings.models import RuntimeSettings
from trader.infra.settings.storage import SaveStorageOverrides
from trader.infra.storage_probe import StorageProbe
from trader.infra.storage_types import StorageLocation, StorageOverride

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
        ("eligibility", paths.eligibility),
        ("budget", paths.budget),
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
    locations.extend((f"external.{index}", path) for index, path in enumerate(additional_paths))
    return tuple(StorageLocation(role, path, probe.inspect(path)) for role, path in locations)


def ConfigureStorage(
    settings: RuntimeSettings,
    *,
    additional_paths: Sequence[Path] = (),
    choose: bool = False,
    report: bool = True,
) -> RuntimeSettings:
    """Resolve terminal choices before runtime resources use the storage profile."""
    locations = InspectDataStorage(
        settings, StorageProbe(settings.storage_overrides), additional_paths=additional_paths
    )
    if choose:
        choices = PromptUnknownStorage(locations, settings.project_root)
        if choices:
            SaveStorageOverrides(settings.config_path, settings.project_root, settings.storage_overrides, choices)
            print("[存储选择] 已保存到运行配置；下次启动将复用此选择。", file=sys.stderr)
            settings = LoadRuntimeSettings(settings.config_path)
            locations = InspectDataStorage(
                settings, StorageProbe(settings.storage_overrides), additional_paths=additional_paths
            )
    if report:
        ReportStorageProfiles(settings, locations)
    return settings


def ReportStorageProfiles(settings: RuntimeSettings, locations: tuple[StorageLocation, ...]) -> None:
    """Group warnings by device and effective override; keep command stdout machine-readable."""
    groups: dict[tuple[str, str, str], list[StorageLocation]] = {}
    for item in locations:
        key = (item.profile.device, item.profile.kind, item.profile.source)
        groups.setdefault(key, []).append(item)
    for items in groups.values():
        profile = items[0].profile
        source = "已使用手动配置" if profile.source == "configuration" else "自动检测"
        print(f"[存储检测] {profile.kind.upper()}（{source}）", file=sys.stderr)
        if profile.kind != "unknown":
            continue
        names = ", ".join(DisplayStoragePath(item.path, settings.project_root) for item in items)
        # Every listed entry is an exact scope: never suggest a broad override across separate mounts.
        example = json.dumps(
            {"path": DisplayStoragePath(items[0].path, settings.project_root), "kind": "hdd"},
            ensure_ascii=False,
        )
        print(
            "[存储配置需修改] "
            f"原因：{_REASONS.get(profile.reason, profile.reason)}。unknown按保守策略处理。\n"
            f"受影响目录：{names}\n"
            f"请修改 {DisplayStoragePath(settings.config_path, settings.project_root)} 的 storage.overrides，"
            f"为上述实际数据目录添加条目，例如：{example}\n"
            "机械硬盘填写 hdd，固态硬盘填写 ssd；路径相对项目根目录，最具体路径优先。保存后重新启动命令生效。",
            file=sys.stderr,
        )


def PromptUnknownStorage(
    locations: tuple[StorageLocation, ...],
    project_root: Path,
    *,
    input_stream: TextIO | None = None,
) -> tuple[StorageOverride, ...]:
    """Ask once per unknown device when a terminal can provide a durable choice."""
    source = input_stream if input_stream is not None else sys.stdin
    if not source.isatty():
        return ()
    groups: dict[str, list[StorageLocation]] = {}
    for item in locations:
        if item.profile.kind == "unknown":
            device = item.profile.device if item.profile.device != "unresolved" else f"unresolved:{item.role}"
            groups.setdefault(device, []).append(item)
    choices: list[StorageOverride] = []
    for device, items in groups.items():
        example = DisplayStoragePath(items[0].path, project_root)
        print(
            f"[存储选择] 设备 {device} 的物理介质无法自动确认，涉及 {len(items)} 个路径，例如 {example}。"
            "请按实际存储介质选择；虚拟盘需在宿主系统确认。",
            file=sys.stderr,
        )
        kind: Literal["ssd", "hdd"] | None = None
        while True:
            print("请选择 1=SSD 固态硬盘、2=HDD 机械硬盘；回车暂不设置：", end="", file=sys.stderr, flush=True)
            answer = source.readline()
            if not answer:
                return tuple(choices)
            selected = answer.strip().lower()
            if selected in {"1", "ssd"}:
                kind = "ssd"
                break
            if selected in {"2", "hdd"}:
                kind = "hdd"
                break
            if not selected:
                break
            print("请输入 1、2 或直接回车。", file=sys.stderr)
        if kind is not None:
            choices.extend(StorageOverride(item.path, kind) for item in items)
    return tuple(choices)


def DisplayStoragePath(path: Path, project_root: Path) -> str:
    return path.relative_to(project_root).as_posix() if path.is_relative_to(project_root) else str(path)
