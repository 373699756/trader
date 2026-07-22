"""Tushare SDK boundary and record-to-observation conversion helpers."""

from __future__ import annotations

import hashlib
import importlib
import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from datetime import time as datetime_time
from types import ModuleType
from typing import Any, cast
from zoneinfo import ZoneInfo

import requests

from trader.application.cache import canonical_json_bytes
from trader.infrastructure.market_data.observations import JsonScalar, SourceObservation

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_DAY_END = datetime_time(23, 59, 59)
_CALENDAR_CHUNK_DAYS = 3650
_MAX_CALENDAR_CHUNKS = 8


class _SdkFacade:
    def __init__(self, module: ModuleType, pro: object, token: str, timeout_seconds: float) -> None:
        self._module = module
        self._pro = pro
        self._token = token
        self._timeout_seconds = timeout_seconds
        self._session = requests.Session()
        self._session.trust_env = False

    def __getattr__(self, name: str) -> object:
        return getattr(self._pro, name)

    def pro_bar(self, **arguments: object) -> object:
        return self._module.pro_bar(api=self._pro, **arguments)

    def daily(self, **arguments: object) -> object:
        params = dict(arguments)
        fields = str(params.pop("fields", ""))
        response = self._session.post(
            "https://api.tushare.pro",
            json=cast(
                Any,
                {
                    "api_name": "daily",
                    "token": self._token,
                    "params": params,
                    "fields": fields,
                },
            ),
            timeout=self._timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise ValueError("Tushare API response must be an object")
        if payload.get("code") != 0:
            if payload.get("code") == 40203:
                raise PermissionError("Tushare API permission denied")
            raise RuntimeError("Tushare API returned an error")
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise ValueError("Tushare API data must be an object")
        columns = data.get("fields")
        items = data.get("items")
        if not isinstance(columns, list) or not isinstance(items, list):
            raise ValueError("Tushare API data must be record-oriented")
        names = tuple(str(column) for column in columns)
        return [dict(zip(names, row, strict=False)) for row in items if isinstance(row, list)]


def _default_sdk_factory(token: str, timeout_seconds: float) -> object:
    module = importlib.import_module("tushare")
    pro_api = module.pro_api
    return _SdkFacade(module, pro_api(token, timeout=timeout_seconds), token, timeout_seconds)


def _invoke(client: object, method: str, **arguments: object) -> object:
    function = getattr(client, method)
    return function(**arguments)


def _records(value: object) -> list[Mapping[str, object]]:
    if hasattr(value, "to_dict"):
        raw = value.to_dict(orient="records")
    else:
        raw = value
    if not isinstance(raw, list):
        raise ValueError("Tushare SDK response must be record-oriented")
    return [row for row in raw if isinstance(row, Mapping)]


def _security_master_observation(
    row: Mapping[str, object],
    observed_at: datetime,
    received_at: datetime,
    data_version: str,
) -> SourceObservation | None:
    code = _code_from_row(row)
    if code is None:
        return None
    listing_date = _compact_date(row.get("list_date"))
    board = _board_from_market(str(row.get("market") or ""), code)
    fields: dict[str, JsonScalar] = {
        "name": str(row.get("name") or ""),
        "industry": str(row.get("industry") or ""),
        "board": board,
        "exchange": str(row.get("exchange") or _exchange_for_code(code)),
        "listing_date": listing_date.isoformat() if listing_date is not None else None,
    }
    effective_at = datetime.combine(
        listing_date or observed_at.astimezone(_SHANGHAI).date(),
        datetime_time.min,
        _SHANGHAI,
    )
    return _observation(
        "security_master",
        code,
        fields,
        observed_at,
        received_at,
        effective_at,
        data_version,
        missing_reasons={
            "is_relisted_first_session": "source_field_unavailable",
            "is_delisting_period_first_session": "source_field_unavailable",
        },
    )


def _calendar_observation(
    row: Mapping[str, object],
    observed_at: datetime,
    received_at: datetime,
    data_version: str,
) -> SourceObservation | None:
    calendar_date = _compact_date(row.get("cal_date"))
    if calendar_date is None:
        return None
    fields: dict[str, JsonScalar] = {
        "exchange": str(row.get("exchange") or "SSE"),
        "calendar_date": calendar_date.isoformat(),
        "is_open": _truthy(row.get("is_open")),
        "previous_trade_date": (_compact_date(row.get("pretrade_date")) or calendar_date).isoformat(),
    }
    effective_at = datetime.combine(calendar_date, datetime_time.min, _SHANGHAI)
    return _observation(
        "trading_calendar",
        calendar_date.isoformat(),
        fields,
        observed_at,
        received_at,
        effective_at,
        data_version,
    )


def _generic_observation(
    dataset: str,
    row: Mapping[str, object],
    observed_at: datetime,
    received_at: datetime,
    data_version: str,
) -> SourceObservation:
    code = _code_from_row(row) or dataset
    fields = {str(key): value for key, raw in row.items() if (value := _json_scalar(raw)) is not None}
    effective_date = _compact_date(row.get("trade_date") or row.get("ann_date") or row.get("end_date"))
    effective_at = datetime.combine(
        effective_date or observed_at.astimezone(_SHANGHAI).date(),
        _DAY_END,
        _SHANGHAI,
    )
    return _observation(dataset, code, fields, observed_at, received_at, effective_at, data_version)


def _observation(
    dataset: str,
    subject_key: str,
    fields: Mapping[str, JsonScalar],
    observed_at: datetime,
    received_at: datetime,
    effective_at: datetime,
    data_version: str,
    *,
    missing_reasons: Mapping[str, str] | None = None,
) -> SourceObservation:
    payload_hash = hashlib.sha256(canonical_json_bytes(fields)).hexdigest()
    return SourceObservation(
        source="tushare",
        subject_key=subject_key,
        observed_at=observed_at,
        source_time=received_at,
        received_at=received_at,
        effective_at=effective_at,
        data_version=data_version,
        fields=fields,
        missing_reasons=missing_reasons or {},
        payload_hash=payload_hash,
        status="success",
        error_code=None,
    )


def _failed_observation(
    dataset: str,
    observed_at: datetime,
    received_at: datetime,
    error_code: str,
    *,
    subject_key: str | None = None,
) -> SourceObservation:
    payload_hash = hashlib.sha256(canonical_json_bytes({"error_code": error_code})).hexdigest()
    return SourceObservation(
        source="tushare",
        subject_key=subject_key or dataset,
        observed_at=observed_at,
        source_time=received_at,
        received_at=received_at,
        effective_at=received_at,
        data_version="tushare-unavailable-v1",
        fields={},
        missing_reasons={dataset: error_code},
        payload_hash=payload_hash,
        status="failed",
        error_code=error_code,
    )


def _data_version(dataset: str, rows: Sequence[Mapping[str, object]]) -> str:
    normalized = [
        {str(key): _json_scalar(value) for key, value in sorted(row.items(), key=lambda item: str(item[0]))}
        for row in rows
    ]
    digest = hashlib.sha256(canonical_json_bytes(normalized)).hexdigest()[:20]
    return f"tushare-{dataset}:{digest}"


def _error_code(exc: Exception) -> str:
    if isinstance(exc, ModuleNotFoundError):
        return "sdk_not_installed"
    if isinstance(exc, PermissionError):
        return "permission_denied"
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return "timeout"
    message = str(exc).lower()
    if "429" in message or "quota" in message or "频次" in message or "权限" in message:
        return "quota_or_rate_limit"
    if "timeout" in message or "timed out" in message:
        return "timeout"
    return "sdk_error"


def _code_from_row(row: Mapping[str, object]) -> str | None:
    value = str(row.get("symbol") or row.get("ts_code") or "").split(".", 1)[0]
    return value if len(value) == 6 and value.isdigit() else None


def _ts_code(code: str) -> str:
    return f"{code}.SH" if code.startswith(("600", "601", "603", "605", "688", "689")) else f"{code}.SZ"


def _exchange_for_code(code: str) -> str:
    return "SSE" if _ts_code(code).endswith(".SH") else "SZSE"


def _board_from_market(market: str, code: str) -> str:
    normalized = market.strip().lower()
    if "创业" in market or "chinext" in normalized or code.startswith(("300", "301")):
        return "chinext"
    if "科创" in market or "star" in normalized or code.startswith(("688", "689")):
        return "star"
    if code.startswith(("000", "001", "002", "003", "600", "601", "603", "605")):
        return "main"
    return "unsupported"


def _compact_date(value: object) -> date | None:
    text = str(value or "").strip().replace("-", "")
    if len(text) != 8 or not text.isdigit():
        return None
    try:
        return datetime.strptime(text, "%Y%m%d").date()
    except ValueError:
        return None


def _json_scalar(value: object) -> JsonScalar:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _truthy(value: object) -> bool:
    return value is True or value == 1 or str(value).strip() == "1"


def _calendar_ranges(start_date: date, end_date: date) -> tuple[tuple[date, date], ...]:
    ranges: list[tuple[date, date]] = []
    cursor = start_date
    while cursor <= end_date:
        if len(ranges) >= _MAX_CALENDAR_CHUNKS:
            raise ValueError("Tushare calendar range exceeds the bounded request limit")
        chunk_end = min(end_date, cursor + timedelta(days=_CALENDAR_CHUNK_DAYS - 1))
        ranges.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return tuple(ranges)


def _percentile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return round(ordered[index], 2)


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Tushare observed_at must be timezone-aware")


__all__ = [
    "_calendar_observation",
    "_calendar_ranges",
    "_data_version",
    "_default_sdk_factory",
    "_error_code",
    "_failed_observation",
    "_generic_observation",
    "_invoke",
    "_percentile",
    "_records",
    "_require_aware",
    "_security_master_observation",
    "_ts_code",
]
