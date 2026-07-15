"""Load the application's single non-secret runtime configuration document."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from typing import TypedDict, cast

from dotenv import load_dotenv

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNTIME_CONFIG_PATH = os.path.join(_PROJECT_ROOT, "config", "runtime.json")
_SECRET_ENV_KEYS = {"DEEPSEEK_API_KEY", "TUSHARE_TOKEN"}
_TRUE_ENV_VALUES = {"1", "true", "yes", "on"}
_FALSE_ENV_VALUES = {"0", "false", "no", "off"}


class RuntimeConfigPayload(TypedDict):
    settings: dict[str, object]
    production_baseline: dict[str, object]
    tuple_settings: list[str]
    env_overrides: list[str]


def _load_runtime_config() -> RuntimeConfigPayload:
    try:
        with open(RUNTIME_CONFIG_PATH, encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception as exc:
        raise RuntimeError(f"runtime configuration is unavailable: {exc}") from exc
    if not isinstance(payload, dict) or int(payload.get("schema_version") or 0) != 1:
        raise RuntimeError("runtime configuration has an unsupported schema")
    settings = payload.get("settings")
    baseline = payload.get("production_baseline")
    tuple_settings = payload.get("tuple_settings") or []
    env_overrides = payload.get("env_overrides") or []
    if not isinstance(settings, dict) or not settings:
        raise RuntimeError("runtime configuration settings must be a non-empty object")
    if not isinstance(baseline, dict) or int(baseline.get("schema_version") or 0) != 1:
        raise RuntimeError("runtime configuration production_baseline is invalid")
    if not isinstance(tuple_settings, list) or any(not isinstance(key, str) for key in tuple_settings):
        raise RuntimeError("runtime configuration tuple_settings must be a string array")
    if not isinstance(env_overrides, list) or any(not isinstance(key, str) for key in env_overrides):
        raise RuntimeError("runtime configuration env_overrides must be a string array")
    invalid_keys = [key for key in settings if not isinstance(key, str) or not key.isupper() or key in _SECRET_ENV_KEYS]
    if invalid_keys:
        raise RuntimeError(f"runtime configuration contains invalid setting keys: {invalid_keys}")
    invalid_overrides = sorted(key for key in env_overrides if key not in settings or key in _SECRET_ENV_KEYS)
    if invalid_overrides:
        raise RuntimeError(f"runtime configuration contains invalid environment override keys: {invalid_overrides}")
    payload["tuple_settings"] = tuple_settings
    payload["env_overrides"] = env_overrides
    return cast(RuntimeConfigPayload, payload)


def _parse_environment_value(name: str, raw_value: str, default: object, is_tuple: bool) -> object:
    value = raw_value.strip()
    if isinstance(default, bool):
        normalized = value.lower()
        if normalized in _TRUE_ENV_VALUES:
            return True
        if normalized in _FALSE_ENV_VALUES:
            return False
        raise RuntimeError(
            "environment variable {} must be one of: {}".format(
                name,
                ", ".join(sorted(_TRUE_ENV_VALUES | _FALSE_ENV_VALUES)),
            )
        )
    if isinstance(default, int):
        try:
            return int(value)
        except ValueError as exc:
            raise RuntimeError(f"environment variable {name} must be an integer") from exc
    if isinstance(default, float):
        try:
            return float(value)
        except ValueError as exc:
            raise RuntimeError(f"environment variable {name} must be a number") from exc
    if is_tuple:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = [item.strip() for item in value.split(",") if item.strip()]
        if not isinstance(parsed, list):
            raise RuntimeError(f"environment variable {name} must be a JSON array or comma-separated list")
        return tuple(parsed)
    if isinstance(default, str):
        return value
    raise RuntimeError(f"environment variable {name} has an unsupported configuration type")


def _frozen_setting_names(baseline: dict[str, object]) -> Iterable[str]:
    for section_name in ("switches", "locked_config"):
        section = baseline.get(section_name)
        if isinstance(section, dict):
            yield from section


def _apply_environment_overrides(payload: RuntimeConfigPayload) -> None:
    settings = payload["settings"]
    baseline = payload["production_baseline"]
    tuple_settings = set(payload["tuple_settings"])
    freeze_enabled = bool(settings.get("PRODUCTION_FREEZE_ENABLED", True))
    frozen_names = set(_frozen_setting_names(baseline)) if freeze_enabled else set()

    for name in sorted(frozen_names):
        raw_value = os.getenv(name)
        if raw_value is None or name not in settings:
            continue
        parsed = _parse_environment_value(name, raw_value, settings[name], name in tuple_settings)
        if parsed != settings[name]:
            raise RuntimeError(f"environment variable {name} cannot override the frozen production baseline")

    for name in payload["env_overrides"]:
        raw_value = os.getenv(name)
        if raw_value is None:
            continue
        globals()[name] = _parse_environment_value(
            name,
            raw_value,
            settings[name],
            name in tuple_settings,
        )


load_dotenv(os.path.join(_PROJECT_ROOT, ".deepseek_key"), override=False)

_RUNTIME_CONFIG: RuntimeConfigPayload = _load_runtime_config()
_TUPLE_SETTINGS: set[str] = set(_RUNTIME_CONFIG["tuple_settings"])
for _name, _value in _RUNTIME_CONFIG["settings"].items():
    if _name in _TUPLE_SETTINGS:
        if not isinstance(_value, list):
            raise RuntimeError(f"runtime configuration tuple setting {_name} must be an array")
        globals()[_name] = tuple(_value)
    else:
        globals()[_name] = _value

_apply_environment_overrides(_RUNTIME_CONFIG)

PRODUCTION_BASELINE_MANIFEST_PATH = RUNTIME_CONFIG_PATH
PRODUCTION_BASELINE_MANIFEST: dict[str, object] = _RUNTIME_CONFIG["production_baseline"]
RUNTIME_ENV_OVERRIDE_KEYS: tuple[str, ...] = tuple(_RUNTIME_CONFIG["env_overrides"])
TUSHARE_TOKEN = os.getenv("TUSHARE_TOKEN", "").strip()

if bool(globals().get("PRODUCTION_FREEZE_ENABLED", True)):
    if str(PRODUCTION_BASELINE_MANIFEST.get("status") or "") != "frozen":
        raise RuntimeError("production baseline must be frozen when production freeze is enabled")

del _name
del _value
