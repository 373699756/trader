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
    locations = InspectDataStorage(settings, StorageProbe(settings.storage_overrides), additional_paths=args.path)
    ReportStorageProfiles(settings, locations)
    unknown = sum(item.profile.kind == "unknown" for item in locations)
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
                    "configuration_field": "storage.overrides",
                    "instruction": "unknown目录需手动配置：机械硬盘填写hdd，固态硬盘填写ssd；保存后重新启动命令。",
                    "override_examples": [
                        {"path": item["path"], "kind": "hdd"} for item in details if item["kind"] == "unknown"
                    ],
                },
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(Main())
