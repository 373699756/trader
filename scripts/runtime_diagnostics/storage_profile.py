"""Read-only storage inventory for the unified diagnostic command."""

import argparse
import json
import sys
from pathlib import Path

from trader.infra.settings import LoadRuntimeSettings
from trader.infra.storage_probe import StorageProbe
from trader.infra.storage_runtime import DisplayStoragePath, InspectDataStorage, ReportStorageProfiles


def Main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--path", type=Path, action="append", default=[])
    args = parser.parse_args()
    settings = LoadRuntimeSettings(args.config)
    locations = InspectDataStorage(settings, StorageProbe(settings.storage_medium), additional_paths=args.path)
    ReportStorageProfiles(settings, locations)
    actual_probe = StorageProbe()
    actual_profiles = tuple(actual_probe.inspect(item.path) for item in locations)
    unknown = sum(item.profile.kind == "unknown" for item in locations)
    unknown_app = any(item.profile.kind == "unknown" and not item.role.startswith("external.") for item in locations)
    actual_unknown = sum(profile.kind == "unknown" for profile in actual_profiles)
    actual_unknown_app = any(
        actual.kind == "unknown" and not item.role.startswith("external.")
        for item, actual in zip(locations, actual_profiles, strict=True)
    )
    configured_mismatches = tuple(
        (item, actual)
        for item, actual in zip(locations, actual_profiles, strict=True)
        if not item.role.startswith("external.")
        and item.profile.source == "configuration"
        and actual.kind != "unknown"
        and item.profile.kind != actual.kind
    )
    configured_mismatch_roles = {item.role for item, _ in configured_mismatches}
    actual_app_kinds = {
        actual.kind
        for item, actual in zip(locations, actual_profiles, strict=True)
        if not item.role.startswith("external.") and actual.kind != "unknown"
    }
    suggested_medium = next(iter(actual_app_kinds)) if len(actual_app_kinds) == 1 else None
    if configured_mismatches:
        expected = suggested_medium.upper() if suggested_medium is not None else "实际介质"
        print(
            "[存储配置需修改] "
            f"runtime storage与物理检测不一致；请改为{expected}后重新启动命令。",
            file=sys.stderr,
        )
    details = [
        {
            "role": item.role,
            "path": DisplayStoragePath(item.path, settings.project_root),
            "kind": item.profile.kind,
            "source": item.profile.source,
            "reason": item.profile.reason,
            "actual_kind": actual.kind,
            "actual_source": actual.source,
            "actual_reason": actual.reason,
            "configured_mismatch": item.role in configured_mismatch_roles,
        }
        for item, actual in zip(locations, actual_profiles, strict=True)
    ]
    if configured_mismatches:
        instruction = "runtime storage与物理介质不一致；按实际介质修改后重新启动命令。"
    elif unknown_app:
        instruction = '统一设置"storage": "ssd"或"storage": "hdd"；保存后重新启动命令。'
    elif actual_unknown_app:
        instruction = "物理介质无法独立验证；手动配置可用于运行策略，但不能作为磁盘性能证据。"
    elif unknown or actual_unknown:
        instruction = "外部目录按实际设备独立检测；请核实该目录所在介质。"
    else:
        instruction = "应用配置与可验证的物理介质一致。"
    configuration_example = (
        {"storage": suggested_medium}
        if configured_mismatches and suggested_medium is not None
        else {"storage": "hdd"} if unknown_app else None
    )
    print(
        json.dumps(
            {
                "schema_version": "storage-profile-v1",
                "status": "degraded" if unknown or actual_unknown or configured_mismatches else "passed",
                "summary": {
                    "locations": details,
                    "unknown": unknown,
                    "actual_unknown": actual_unknown,
                    "configured_mismatches": len(configured_mismatches),
                    "read_only": True,
                    "configuration_file": DisplayStoragePath(settings.config_path, settings.project_root),
                    "configuration_field": "storage",
                    "instruction": instruction,
                    "configuration_example": configuration_example,
                    "storage_policies": {
                        medium: {
                            "connection_timeout_seconds": policy.connection_timeout_seconds,
                            "read_window_rows": policy.read_window_rows,
                            "transaction_batch_rows": policy.transaction_batch_rows,
                            "checkpoint_mode": policy.checkpoint_mode,
                        }
                        for medium, policy in (
                            ("hdd", settings.storage_policy.hdd),
                            ("ssd", settings.storage_policy.ssd),
                        )
                    },
                },
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(Main())
