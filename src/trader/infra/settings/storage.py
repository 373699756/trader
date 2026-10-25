"""Strict parsing of per-directory storage overrides."""

from collections.abc import Mapping
from pathlib import Path

from trader.infra.settings.parser import ConfigurationError, RequireExactKeys, RequireMapping, ResolveProjectPath, Text
from trader.infra.storage_types import StorageOverride


def ParseStorageOverrides(raw: Mapping[str, object], project_root: Path) -> tuple[StorageOverride, ...]:
    RequireExactKeys(raw, {"overrides"}, "storage")
    values = raw["overrides"]
    if not isinstance(values, list) or len(values) > 128:
        raise ConfigurationError("storage.overrides must be a list of at most 128 entries")
    result: list[StorageOverride] = []
    for index, value in enumerate(values):
        label = f"storage.overrides[{index}]"
        item = RequireMapping({label: value}, label)
        RequireExactKeys(item, {"path", "kind"}, label)
        kind = Text(item, "kind")
        if kind not in {"hdd", "ssd"}:
            raise ConfigurationError(f"{label}.kind must be hdd or ssd")
        path = ResolveProjectPath(project_root, Text(item, "path"))
        if any(override.path == path for override in result):
            raise ConfigurationError(f"{label}.path duplicates an existing storage override")
        result.append(StorageOverride(path, "hdd" if kind == "hdd" else "ssd"))
    return tuple(sorted(result, key=lambda item: len(item.path.parts), reverse=True))
