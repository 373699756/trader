"""Small typed primitives used by JSON settings loaders."""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping
from pathlib import Path


class ConfigurationError(ValueError):
    """Raised when a configuration cannot satisfy its contract."""


def ReadJsonObject(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigurationError(f"configuration file does not exist: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"cannot read configuration file {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigurationError(f"configuration root must be an object: {path}")
    return raw


def ReadConfigSections(
    path: Path,
    root_keys: set[str],
    section_files: dict[str, set[str]],
    *,
    label: str,
    optional_root_keys: frozenset[str] = frozenset(),
) -> dict[str, object]:
    """Read one fixed, non-recursive set of sibling configuration sections."""
    root = ReadJsonObject(path)
    RequireExactKeys(root, root_keys | (set(root) & optional_root_keys), label)
    merged = dict(root)
    for filename, section_keys in section_files.items():
        section = ReadJsonObject(path.parent / filename)
        RequireExactKeys(section, section_keys, filename.removesuffix(".json"))
        if set(merged).intersection(section):
            raise ConfigurationError(f"{label} has duplicate configuration sections in {filename}")
        merged.update(section)
    return merged


def InferProjectRoot(config_dir: Path) -> Path:
    # The canonical runtime configuration lives directly under ``config``;
    # project-relative paths (for example ``.runtime/trader``) must therefore
    # Resolve from the project root rather than from the configuration
    # directory itself.
    if config_dir.name == "config":
        return config_dir.parent.resolve()
    return config_dir.resolve()


def ResolveProjectPath(project_root: Path, raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    return path.resolve() if path.is_absolute() else (project_root / path).resolve()


def ResolveConfigPath(config_dir: Path, raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    return path.resolve() if path.is_absolute() else (config_dir / path).resolve()


def RequireMapping(raw: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise ConfigurationError(f"{key} must be an object")
    return value


def ProfileMapping(raw: object, profile_id: str, label: str) -> Mapping[str, object]:
    if not isinstance(raw, list):
        raise ConfigurationError(f"{label} must be a profile list")
    profiles: dict[str, Mapping[str, object]] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise ConfigurationError(f"{label} entries must be objects")
        identity = item.get("profile")
        if not isinstance(identity, str) or identity not in {"v1", "v2", "v3", "v5"} or identity in profiles:
            raise ConfigurationError(f"{label} profile identities must be unique v1, v2, v3 and v5")
        profiles[identity] = {key: value for key, value in item.items() if key != "profile"}
    if not {"v2", "v3"}.issubset(profiles) or profile_id not in profiles:
        raise ConfigurationError(f"{label} must define v2, v3 and the requested profile")
    return profiles[profile_id]


def RequireExactKeys(raw: Mapping[str, object], expected: set[str], label: str) -> None:
    missing = sorted(expected - set(raw))
    unknown = sorted(set(raw) - expected)
    if missing:
        if len(missing) == 1:
            raise ConfigurationError(f"{label}.{missing[0]} is required")
        raise ConfigurationError(f"{label} missing keys: {', '.join(missing)}")
    if unknown:
        raise ConfigurationError(f"{label} contains unknown keys: {', '.join(unknown)}")


def Text(raw: Mapping[str, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{key} must be a non-empty string")
    return value.strip()


def Boolean(raw: Mapping[str, object], key: str) -> bool:
    value = raw.get(key)
    if not isinstance(value, bool):
        raise ConfigurationError(f"{key} must be a boolean")
    return value


def Integer(
    raw: Mapping[str, object],
    key: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    value = raw.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigurationError(f"{key} must be an integer")
    if minimum is not None and value < minimum:
        raise ConfigurationError(f"{key} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ConfigurationError(f"{key} must be at most {maximum}")
    return value


def Number(
    raw: Mapping[str, object],
    key: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    value = raw.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ConfigurationError(f"{key} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ConfigurationError(f"{key} must be a finite number")
    if minimum is not None and result < minimum:
        raise ConfigurationError(f"{key} must be at least {minimum}")
    if maximum is not None and result > maximum:
        raise ConfigurationError(f"{key} must be at most {maximum}")
    return result


def NumberMapping(raw: Mapping[str, object], key: str) -> dict[str, float]:
    values = RequireMapping(raw, key)
    result: dict[str, float] = {}
    for name, value in values.items():
        if not isinstance(name, str) or not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ConfigurationError(f"{key} must contain numeric values")
        number_value = float(value)
        if not math.isfinite(number_value):
            raise ConfigurationError(f"{key} must contain finite numeric values")
        result[name] = number_value
    return result


def IntegerMapping(raw: Mapping[str, object], key: str, *, minimum: int) -> dict[str, int]:
    values = RequireMapping(raw, key)
    result: dict[str, int] = {}
    for name, value in values.items():
        if not isinstance(name, str) or not isinstance(value, int) or isinstance(value, bool) or value < minimum:
            raise ConfigurationError(f"{key} must contain integers >= {minimum}")
        result[name] = value
    return result


def NestedNumberMapping(raw: Mapping[str, object], key: str) -> dict[str, dict[str, float]]:
    values = RequireMapping(raw, key)
    result: dict[str, dict[str, float]] = {}
    for name, nested in values.items():
        if not isinstance(name, str) or not isinstance(nested, dict):
            raise ConfigurationError(f"{key} must contain objects")
        result[name] = NumberMapping({"values": nested}, "values")
    return result


def TripleNestedNumberMapping(raw: Mapping[str, object], key: str) -> dict[str, dict[str, dict[str, float]]]:
    values = RequireMapping(raw, key)
    result: dict[str, dict[str, dict[str, float]]] = {}
    for name, nested in values.items():
        if not isinstance(name, str) or not isinstance(nested, dict):
            raise ConfigurationError(f"{key} must contain objects")
        result[name] = NestedNumberMapping({"values": nested}, "values")
    return result


def EnvironmentInteger(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if not 1 <= value <= 65535:
        raise ConfigurationError(f"{name} must be between 1 and 65535")
    return value
