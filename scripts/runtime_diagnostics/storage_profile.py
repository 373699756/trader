"""Read-only storage inventory for the unified diagnostic command."""

import argparse
import json
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
    unknown = sum(item.profile.kind == "unknown" for item in locations)
    unknown_app = any(item.profile.kind == "unknown" and not item.role.startswith("external.") for item in locations)
    details = [
        {
            "role": item.role,
            "path": DisplayStoragePath(item.path, settings.project_root),
            "kind": item.profile.kind,
            "source": item.profile.source,
            "reason": item.profile.reason,
        }
        for item in locations
    ]
    print(
        json.dumps(
            {
                "schema_version": "storage-profile-v1",
                "status": "degraded" if unknown else "passed",
                "summary": {
                    "locations": details,
                    "unknown": unknown,
                    "read_only": True,
                    "configuration_file": DisplayStoragePath(settings.config_path, settings.project_root),
                    "configuration_field": "storage",
                    "instruction": (
                        '统一设置"storage": "ssd"或"storage": "hdd"；保存后重新启动命令。'
                        if unknown_app else "外部目录按实际设备独立检测；请核实该目录所在介质。"
                    ),
                    "configuration_example": {"storage": "hdd"} if unknown_app else None,
                },
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(Main())
