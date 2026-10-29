"""Parse and atomically save the single global storage medium."""

import json
from pathlib import Path

from trader.infra.serialization.sealing import ReplaceFile
from trader.infra.settings.parser import ConfigurationError, ReadJsonObject
from trader.infra.storage_types import StorageMedium


def ParseStorageMedium(raw: object) -> StorageMedium | None:
    if raw is None:
        return None
    if raw == "hdd":
        return "hdd"
    if raw == "ssd":
        return "ssd"
    raise ConfigurationError('storage must be "ssd" or "hdd"; omit it or use null for automatic detection')


def SaveStorageMedium(config_path: Path, expected: StorageMedium | None, selected: StorageMedium) -> None:
    """Remember one terminal choice without replacing other configuration fields."""
    raw = ReadJsonObject(config_path)
    if ParseStorageMedium(raw.get("storage")) != expected:
        raise ConfigurationError("storage changed while selecting storage")
    raw["storage"] = ParseStorageMedium(selected)
    ReplaceFile(config_path, json.dumps(raw, ensure_ascii=False, indent=2) + "\n")
