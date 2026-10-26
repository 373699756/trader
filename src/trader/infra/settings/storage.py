"""Strict parsing of per-directory storage overrides."""

import json
from collections.abc import Mapping
from pathlib import Path

from trader.infra.serialization.sealing import ReplaceFile
from trader.infra.settings.parser import (
    ConfigurationError,
    ReadJsonObject,
    RequireExactKeys,
    RequireMapping,
    ResolveProjectPath,
    Text,
)
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


def SaveStorageOverrides(
    config_path: Path,
    project_root: Path,
    expected: tuple[StorageOverride, ...],
    additions: tuple[StorageOverride, ...],
) -> None:
    """Atomically remember terminal choices in the existing runtime configuration."""
    raw = ReadJsonObject(config_path)
    section = raw.get("storage", {"overrides": []})
    if not isinstance(section, dict):
        raise ConfigurationError("storage must be an object")
    if ParseStorageOverrides(section, project_root) != expected:
        raise ConfigurationError("storage.overrides changed while selecting storage")
    entries = section["overrides"]
    if not isinstance(entries, list):
        raise ConfigurationError("storage.overrides must be a list")
    updated = list(entries)
    for choice in additions:
        try:
            path = choice.path.relative_to(project_root).as_posix()
        except ValueError:
            path = str(choice.path)
        updated.append({"path": path, "kind": choice.kind})
    replacement = {"overrides": updated}
    ParseStorageOverrides(replacement, project_root)
    raw["storage"] = replacement
    ReplaceFile(config_path, json.dumps(raw, ensure_ascii=False, indent=2) + "\n")
