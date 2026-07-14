"""Load the application's single non-secret runtime configuration document."""

from __future__ import annotations

import json
import os
from typing import Dict

from dotenv import load_dotenv


_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNTIME_CONFIG_PATH = os.path.join(_PROJECT_ROOT, "config", "runtime.json")
_SECRET_ENV_KEYS = {"DEEPSEEK_API_KEY", "TUSHARE_TOKEN"}


def _load_runtime_config() -> Dict[str, object]:
    try:
        with open(RUNTIME_CONFIG_PATH, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception as exc:
        raise RuntimeError("runtime configuration is unavailable: {}".format(exc)) from exc
    if not isinstance(payload, dict) or int(payload.get("schema_version") or 0) != 1:
        raise RuntimeError("runtime configuration has an unsupported schema")
    settings = payload.get("settings")
    baseline = payload.get("production_baseline")
    tuple_settings = payload.get("tuple_settings") or []
    if not isinstance(settings, dict) or not settings:
        raise RuntimeError("runtime configuration settings must be a non-empty object")
    if not isinstance(baseline, dict) or int(baseline.get("schema_version") or 0) != 1:
        raise RuntimeError("runtime configuration production_baseline is invalid")
    if not isinstance(tuple_settings, list) or any(not isinstance(key, str) for key in tuple_settings):
        raise RuntimeError("runtime configuration tuple_settings must be a string array")
    invalid_keys = [
        key
        for key in settings
        if not isinstance(key, str) or not key.isupper() or key in _SECRET_ENV_KEYS
    ]
    if invalid_keys:
        raise RuntimeError("runtime configuration contains invalid setting keys: {}".format(invalid_keys))
    payload["tuple_settings"] = tuple_settings
    return payload


load_dotenv(os.path.join(_PROJECT_ROOT, ".deepseek_key"), override=False)

_RUNTIME_CONFIG = _load_runtime_config()
_TUPLE_SETTINGS = set(_RUNTIME_CONFIG["tuple_settings"])
for _name, _value in _RUNTIME_CONFIG["settings"].items():
    globals()[_name] = tuple(_value) if _name in _TUPLE_SETTINGS else _value

PRODUCTION_BASELINE_MANIFEST_PATH = RUNTIME_CONFIG_PATH
PRODUCTION_BASELINE_MANIFEST = _RUNTIME_CONFIG["production_baseline"]
TUSHARE_TOKEN = os.getenv("TUSHARE_TOKEN", "").strip()

if bool(globals().get("PRODUCTION_FREEZE_ENABLED", True)):
    if str(PRODUCTION_BASELINE_MANIFEST.get("status") or "") != "frozen":
        raise RuntimeError("production baseline must be frozen when production freeze is enabled")

del _name
del _value
