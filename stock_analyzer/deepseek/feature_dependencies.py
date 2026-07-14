from __future__ import annotations

import json
import os
import re
import time
from typing import Dict

import requests

from .. import config
from .http_client import DeepSeekHttpClient
from .news_context import NewsContextProvider
from .payload_builder import PayloadBuilder


_DEFAULT_BASE_URL = "https://api.deepseek.com"
_DOTENV_LOADED = False


def feature_runtime_config() -> Dict[str, object]:
    _load_dotenv_if_needed()
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    return {
        "enabled": bool(getattr(config, "ENABLE_DEEPSEEK_RUNTIME", True))
        and _env_bool("DEEPSEEK_ENABLED", bool(api_key)),
        "base_url": _coerce_base_url(os.getenv("DEEPSEEK_BASE_URL", _DEFAULT_BASE_URL)),
        "api_key": api_key,
        "model": str(getattr(config, "DEEPSEEK_FEATURE_MODEL", "deepseek-v4-flash")),
        "pro_model": str(os.getenv("DEEPSEEK_PRO_MODEL", "deepseek-v4-pro") or "deepseek-v4-pro"),
        "max_tokens": max(80, _env_int("DEEPSEEK_MAX_TOKENS", 800)),
        "timeout_seconds": max(2.0, _env_float("DEEPSEEK_TIMEOUT_SECONDS", 12.0)),
    }


def safe_parse_json(text: str):
    value = str(text or "").strip().lstrip("\ufeff")
    if not value:
        return None
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE).strip()
        value = re.sub(r"\s*```$", "", value).strip()
    decoder = json.JSONDecoder()
    candidates = [value]
    for start in (index for index, char in enumerate(value) if char in "[{"):
        try:
            parsed, _ = decoder.raw_decode(value[start:])
        except Exception:
            continue
        if isinstance(parsed, (dict, list)):
            return parsed
    match = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", value)
    if match:
        candidates.append(match.group(1))
    for candidate in candidates:
        for current in (candidate, re.sub(r",(\s*[}\]])", r"\1", candidate)):
            try:
                return json.loads(current)
            except Exception:
                continue
    return None


def deepseek_chat_url(base_url: str) -> str:
    return "{}/chat/completions".format(_coerce_base_url(base_url).rstrip("/"))


def _coerce_base_url(value: str) -> str:
    url = str(value or "").strip().rstrip("/") or _DEFAULT_BASE_URL
    lower = url.lower()
    if lower.endswith("/chat/completions"):
        return url[: -len("/chat/completions")].rstrip("/") or _DEFAULT_BASE_URL
    if lower.endswith("/v1"):
        return url[: -len("/v1")].rstrip("/") or _DEFAULT_BASE_URL
    return url


def _load_dotenv_if_needed() -> None:
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    _DOTENV_LOADED = True
    configured = os.path.expanduser(os.getenv("DEEPSEEK_ENV_FILE", ".env").strip() or ".env")
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates = [configured] if os.path.isabs(configured) else [
        os.path.abspath(configured),
        os.path.join(project_root, configured),
    ]
    path = next((item for item in candidates if os.path.exists(item)), "")
    if not path:
        return
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if key and key not in os.environ:
                    os.environ[key] = value.strip().strip('"').strip("'")
    except Exception:
        return


def _env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, "1" if default else "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)).strip())
    except Exception:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except Exception:
        return default


def _market_data_provider_factory():
    from ..providers import MarketDataProvider

    return MarketDataProvider()


def _score_news_items(items):
    from ..sentiment import score_news_items

    return score_news_items(items)


FEATURE_HTTP_CLIENT = DeepSeekHttpClient(requests_module=requests, sleep_func=time.sleep)
FEATURE_NEWS_CONTEXT_PROVIDER = NewsContextProvider(
    config_module=config,
    time_module=time,
    provider_factory=_market_data_provider_factory,
    score_news_items=_score_news_items,
)
FEATURE_PAYLOAD_BUILDER = PayloadBuilder()
