"""Pure parsing helpers for the AKShare-compatible adapter."""

from __future__ import annotations

import hashlib
import html
import json
import logging
import math
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, time
from typing import Protocol
from zoneinfo import ZoneInfo

import requests

_LOGGER = logging.getLogger(__name__)


class HttpResponse(Protocol):
    text: str

    def raise_for_status(self) -> None: ...

    def json(self) -> object: ...


GetFunction = Callable[..., HttpResponse]
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
_DIRECT_PROXIES = {"http": "", "https": "", "all": ""}
_SOURCE_EXCEPTIONS = (OSError, RuntimeError, ValueError, requests.RequestException)


def _point_in_time(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("research observation time must be timezone-aware")
    return value.astimezone(SHANGHAI_TZ)


def _validate_code(code: str) -> None:
    if len(code) != 6 or not code.isdigit():
        raise ValueError("research stock code must contain exactly six digits")


def _first_text(row: Mapping[str, object], keys: Sequence[str]) -> str:
    for key in keys:
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def _news_rows(content: str, callback: str) -> list[Mapping[str, object]]:
    content = content.strip()
    if content.endswith(";"):
        content = content[:-1]
    prefix = f"{callback}("
    if not content.startswith(prefix) or not content.endswith(")"):
        raise RuntimeError("AKShare news response is not valid JSONP")
    try:
        payload = json.loads(content[len(prefix) : -1])
    except json.JSONDecodeError as exc:
        raise RuntimeError("AKShare news response contains invalid JSON") from exc
    if not isinstance(payload, dict):
        return []
    result = payload.get("result")
    rows = result.get("cmsArticleWebOld") if isinstance(result, dict) else None
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _result_rows(payload: Mapping[str, object]) -> list[Mapping[str, object]]:
    if payload.get("success") is False and payload.get("code") != 9201:
        raise RuntimeError("research source reported a failed response")
    result = payload.get("result")
    if result is None and payload.get("code") == 9201:
        return []
    if not isinstance(result, dict):
        raise RuntimeError("research source result is missing")
    rows = result.get("data")
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise RuntimeError("research source rows are invalid")
    if any(not isinstance(row, dict) for row in rows):
        raise RuntimeError("research source contains a malformed row")
    return rows


def _announcement_rows(payload: Mapping[str, object]) -> list[Mapping[str, object]]:
    if payload.get("success") != 1:
        raise RuntimeError("announcement source reported a failed response")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("announcement source data is missing")
    rows = data.get("list")
    if not isinstance(rows, list):
        raise RuntimeError("announcement source rows are invalid")
    if any(not isinstance(row, dict) for row in rows):
        raise RuntimeError("announcement source contains a malformed row")
    return rows


def _clean_text(value: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", value)).strip()


def _parse_datetime(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("/", "-").replace("Z", "+00:00"))
    except ValueError:
        return None
    return _as_shanghai_time(parsed)


def _parse_precise_datetime(raw: object) -> datetime | None:
    value = str(raw or "").strip()
    if not value:
        return None
    value = re.sub(r":(\d{3})$", r".\1", value).replace("/", "-")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return _as_shanghai_time(parsed)


def _parse_date(raw: object) -> date | None:
    value = str(raw or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10].replace("/", "-"))
    except ValueError:
        return None


def _parse_date_end(raw: object) -> datetime | None:
    parsed = _parse_date(raw)
    return datetime.combine(parsed, time(23, 59, 59), SHANGHAI_TZ) if parsed is not None else None


def _as_shanghai_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=SHANGHAI_TZ)
    return value.astimezone(SHANGHAI_TZ)


def _finite_number(raw: object) -> float | None:
    if not isinstance(raw, (str, int, float)) or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _payload_version(prefix: str, payload: Mapping[str, object]) -> str:
    version = str(payload.get("version") or "").strip()
    if version:
        return f"{prefix}:{version[:64]}"
    try:
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("research payload cannot be versioned") from exc
    return _content_version(prefix, canonical)


def _content_version(prefix: str, content: str) -> str:
    return f"{prefix}:sha256:{hashlib.sha256(content.encode()).hexdigest()[:20]}"


def _source_error(source: str, error: BaseException) -> str:
    return f"{source}:{type(error).__name__}"


def _summary_number(value: float | None) -> str:
    return "null" if value is None else f"{value:.6g}"
