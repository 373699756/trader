"""Parse and atomically save the single global disk medium."""

import json
from pathlib import Path

from trader.infra.serialization.sealing import ReplaceFile
from trader.infra.settings.parser import ConfigurationError, ReadJsonObject
from trader.infra.disk_types import DiskMedium


def ParseDiskMedium(raw: object) -> DiskMedium | None:
    if raw is None:
        return None
    if raw == "hdd":
        return "hdd"
    if raw == "ssd":
        return "ssd"
    raise ConfigurationError('disk must be "ssd" or "hdd"; omit it or use null for automatic detection')


def SaveDiskMedium(config_path: Path, expected: DiskMedium | None, selected: DiskMedium) -> None:
    """Remember one terminal choice without replacing other configuration fields."""
    raw = ReadJsonObject(config_path)
    if ParseDiskMedium(raw.get("disk_medium")) != expected:
        raise ConfigurationError("disk changed while selecting disk")
    raw["disk_medium"] = ParseDiskMedium(selected)
    ReplaceFile(config_path, json.dumps(raw, ensure_ascii=False, indent=2) + "\n")
