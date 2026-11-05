"""Read-only disk inventory for the unified diagnostic command."""

from scripts.core.paths import PROJECT_ROOT as TOOL_ROOT

import argparse
import json
import sys
from pathlib import Path

from trader.infra.settings import LoadRuntimeSettings
from trader.infra.disk_probe import DiskProbe
from trader.infra.disk_runtime import DisplayDiskPath, InspectDataDisk, ReportDiskProfiles
from scripts.infra.disk.disk_policy_audit import AuditSqlitePolicyConsumers


def Main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--path", type=Path, action="append", default=[])
    parser.add_argument("--policy-audit", action="store_true")
    args = parser.parse_args()
    settings = LoadRuntimeSettings(args.config)
    locations = InspectDataDisk(settings, DiskProbe(settings.disk_medium), additional_paths=args.path)
    ReportDiskProfiles(settings, locations)
    actual_probe = DiskProbe()
    actual_profiles = tuple(actual_probe.inspect(item.path) for item in locations)
    policy_audit = AuditSqlitePolicyConsumers(TOOL_ROOT) if args.policy_audit else None
    if policy_audit is not None:
        from trader.bootstrap import BuildSqlitePolicy

        selected_policy = BuildSqlitePolicy(settings)
        policy_audit["configured_medium"] = settings.disk_medium
        policy_audit["selected_sqlite_policy"] = {
            "connection_timeout_seconds": selected_policy.connection_timeout_seconds,
            "read_window_rows": selected_policy.read_window_rows,
            "transaction_batch_rows": selected_policy.transaction_batch_rows,
            "checkpoint_mode": selected_policy.checkpoint_mode,
        }
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
            f"runtime.json.disk_medium与物理检测不一致；请改为{expected}后重新启动命令。",
            file=sys.stderr,
        )
    details = [
        {
            "role": item.role,
            "path": DisplayDiskPath(item.path, settings.project_root),
            "kind": item.profile.kind,
            "source": item.profile.source,
            "reason": item.profile.reason,
            "actual_kind": actual.kind,
            "actual_device": actual.device,
            "actual_source": actual.source,
            "actual_reason": actual.reason,
            "configured_mismatch": item.role in configured_mismatch_roles,
        }
        for item, actual in zip(locations, actual_profiles, strict=True)
    ]
    if configured_mismatches:
        instruction = "runtime.json.disk_medium与物理介质不一致；按实际介质修改后重新启动命令。"
    elif unknown_app:
        instruction = '统一设置"disk_medium": "ssd"或"disk_medium": "hdd"；保存后重新启动命令。'
    elif actual_unknown_app:
        instruction = "物理介质无法独立验证；手动配置可用于运行策略，但不能作为磁盘性能证据。"
    elif unknown or actual_unknown:
        instruction = "外部目录按实际设备独立检测；请核实该目录所在介质。"
    else:
        instruction = "应用配置与可验证的物理介质一致。"
    configuration_example = (
        {"disk_medium": suggested_medium}
        if configured_mismatches and suggested_medium is not None
        else {"disk_medium": "hdd"} if unknown_app else None
    )
    print(
        json.dumps(
            {
                "schema_version": "disk-profile-v1",
                "status": "degraded" if unknown or actual_unknown or configured_mismatches or (
                    policy_audit is not None and (
                        policy_audit["missing_policy_calls"] or policy_audit["unpacked_policy_calls_require_review"]
                    )
                ) else "passed",
                "summary": {
                    "locations": details,
                    "unknown": unknown,
                    "actual_unknown": actual_unknown,
                    "configured_mismatches": len(configured_mismatches),
                    "read_only": True,
                    "configuration_file": DisplayDiskPath(settings.config_path, settings.project_root),
                    "configuration_field": "disk_medium",
                    "instruction": instruction,
                    "configuration_example": configuration_example,
                    "policy_consumers": policy_audit,
                    "sqlite_policies": {
                        medium: {
                            "connection_timeout_seconds": policy.connection_timeout_seconds,
                            "read_window_rows": policy.read_window_rows,
                            "transaction_batch_rows": policy.transaction_batch_rows,
                            "checkpoint_mode": policy.checkpoint_mode,
                        }
                        for medium, policy in (
                            ("hdd", settings.sqlite_policy.hdd),
                            ("ssd", settings.sqlite_policy.ssd),
                        )
                    },
                },
            },
            ensure_ascii=False, indent=2,
        )
    )
    return 0
