"""Small typed primitives used by v2 JSON settings loaders."""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping
from pathlib import Path


class ConfigurationError(ValueError):
    """Raised when a v2 configuration cannot satisfy its contract."""


def read_json_object(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigurationError(f"configuration file does not exist: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"cannot read configuration file {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigurationError(f"configuration root must be an object: {path}")
    return raw


def infer_project_root(config_dir: Path) -> Path:
    if config_dir.name == "v2" and config_dir.parent.name == "config":
        return config_dir.parent.parent.resolve()
    return config_dir.resolve()


def resolve_project_path(project_root: Path, raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    return path.resolve() if path.is_absolute() else (project_root / path).resolve()


def resolve_config_path(config_dir: Path, raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    return path.resolve() if path.is_absolute() else (config_dir / path).resolve()


def mapping(raw: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise ConfigurationError(f"{key} must be an object")
    return value


def text(raw: Mapping[str, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{key} must be a non-empty string")
    return value.strip()


def boolean(raw: Mapping[str, object], key: str) -> bool:
    value = raw.get(key)
    if not isinstance(value, bool):
        raise ConfigurationError(f"{key} must be a boolean")
    return value


def integer(
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


def number(
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


def number_mapping(raw: Mapping[str, object], key: str) -> dict[str, float]:
    values = mapping(raw, key)
    result: dict[str, float] = {}
    for name, value in values.items():
        if not isinstance(name, str) or not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ConfigurationError(f"{key} must contain numeric values")
        number_value = float(value)
        if not math.isfinite(number_value):
            raise ConfigurationError(f"{key} must contain finite numeric values")
        result[name] = number_value
    return result


def integer_mapping(raw: Mapping[str, object], key: str, *, minimum: int) -> dict[str, int]:
    values = mapping(raw, key)
    result: dict[str, int] = {}
    for name, value in values.items():
        if not isinstance(name, str) or not isinstance(value, int) or isinstance(value, bool) or value < minimum:
            raise ConfigurationError(f"{key} must contain integers >= {minimum}")
        result[name] = value
    return result


def nested_number_mapping(raw: Mapping[str, object], key: str) -> dict[str, dict[str, float]]:
    values = mapping(raw, key)
    result: dict[str, dict[str, float]] = {}
    for name, nested in values.items():
        if not isinstance(name, str) or not isinstance(nested, dict):
            raise ConfigurationError(f"{key} must contain objects")
        result[name] = number_mapping({"values": nested}, "values")
    return result


def environment_integer(name: str, default: int) -> int:
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
