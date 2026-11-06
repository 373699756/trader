"""Bounded CNINFO industry queries with immutable native classification evidence."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from time import monotonic

from trader.infra.http_contracts import HttpCancelled, HttpParameter, HttpTimeout, HttpTransport

_BASE_URL = "https://webapi.cninfo.com.cn/api/stock/"
_STANDARD_CODE = "008001"
_STANDARD_NAME = "中国上市公司协会上市公司行业分类标准"
_MAX_BYTES = 2 * 1024 * 1024
_MAX_ROWS = 20_000


class CninfoIndustryError(RuntimeError):
    """The response cannot establish complete current-standard industry evidence."""


@dataclass(frozen=True)
class CninfoIndustryCategory:
    code: str
    parent_code: str
    name: str
    english_name: str | None
    ended_on: date | None
    standard_code: str
    standard_name: str


@dataclass(frozen=True)
class CninfoIndustryChange:
    code: str
    company_name: str
    organization_name: str
    changed_on: date
    standard_code: str
    standard_name: str
    industry_code: str
    section_name: str
    subcategory_name: str | None
    category_name: str | None
    middle_name: str | None
    latest_marker: str | int

    @property
    def is_latest(self) -> bool:
        return self.latest_marker in (1, "1")

    @property
    def is_current_standard(self) -> bool:
        return self.standard_code == _STANDARD_CODE and self.standard_name == _STANDARD_NAME


@dataclass(frozen=True)
class CninfoIndustryTaxonomy:
    rows: tuple[CninfoIndustryCategory, ...]
    response_sha256: str
    response_content: bytes


@dataclass(frozen=True)
class CninfoIndustryChanges:
    rows: tuple[CninfoIndustryChange, ...]
    response_sha256: str
    response_content: bytes


def _string(record: dict[str, object], field: str, *, maximum: int = 200) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise CninfoIndustryError("cninfo_industry_field_invalid")
    return value.strip()


def _optional_string(record: dict[str, object], field: str, *, maximum: int = 200) -> str | None:
    if field not in record:
        raise CninfoIndustryError("cninfo_industry_field_missing")
    value = record[field]
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > maximum:
        raise CninfoIndustryError("cninfo_industry_field_invalid")
    return value.strip() or None


def _day(value: str) -> date:
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None:
        raise CninfoIndustryError("cninfo_industry_date_invalid")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise CninfoIndustryError("cninfo_industry_date_invalid") from exc


def _require_standard(code: str, name: str) -> None:
    if code != _STANDARD_CODE or name != _STANDARD_NAME:
        raise CninfoIndustryError("cninfo_industry_standard_unexpected")


def _category(record: dict[str, object]) -> CninfoIndustryCategory:
    standard_code = _string(record, "F003V")
    standard_name = _string(record, "F004V")
    _require_standard(standard_code, standard_name)
    ended = _optional_string(record, "F002D")
    return CninfoIndustryCategory(
        _string(record, "SORTCODE", maximum=20),
        _string(record, "PARENTCODE", maximum=20),
        _string(record, "SORTNAME"),
        _optional_string(record, "F001V", maximum=500),
        _day(ended) if ended is not None else None,
        standard_code,
        standard_name,
    )


def _change(record: dict[str, object], code: str, start_date: date, end_date: date) -> CninfoIndustryChange:
    standard_code = _string(record, "F001V")
    standard_name = _string(record, "F002V")
    changed_on = _day(_string(record, "VARYDATE"))
    if _string(record, "SECCODE") != code or not start_date <= changed_on <= end_date:
        raise CninfoIndustryError("cninfo_industry_response_identity_invalid")
    latest = record.get("F008C")
    if not isinstance(latest, (int, str)) or isinstance(latest, bool) or latest not in (0, 1, "0", "1"):
        raise CninfoIndustryError("cninfo_industry_latest_marker_invalid")
    return CninfoIndustryChange(
        code,
        _string(record, "SECNAME"),
        _string(record, "ORGNAME", maximum=500),
        changed_on,
        standard_code,
        standard_name,
        _string(record, "F003V", maximum=20),
        _string(record, "F004V"),
        _optional_string(record, "F005V"),
        _optional_string(record, "F006V"),
        _optional_string(record, "F007V"),
        latest,
    )


def _check_cancel(cancel_requested: Callable[[], bool]) -> None:
    if cancel_requested():
        raise HttpCancelled("cninfo_industry_cancelled")


def _records(content: bytes, cancel_requested: Callable[[], bool]) -> tuple[dict[str, object], ...]:
    _check_cancel(cancel_requested)
    if not content or len(content) > _MAX_BYTES:
        raise CninfoIndustryError("cninfo_industry_response_size_invalid")
    try:
        payload: object = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CninfoIndustryError("cninfo_industry_json_invalid") from exc
    if not isinstance(payload, dict) or type(payload.get("resultcode")) is not int or payload["resultcode"] != 200:
        raise CninfoIndustryError("cninfo_industry_query_rejected")
    records = payload.get("records")
    if (
        not isinstance(records, list) or len(records) > _MAX_ROWS
        or type(payload.get("count")) is not int or payload["count"] != len(records)
    ):
        raise CninfoIndustryError("cninfo_industry_records_incomplete")
    validated: list[dict[str, object]] = []
    for record in records:
        _check_cancel(cancel_requested)
        if not isinstance(record, dict) or any(not isinstance(key, str) for key in record):
            raise CninfoIndustryError("cninfo_industry_record_invalid")
        validated.append(record)
    return tuple(validated)


def _validate_category_tree(rows: tuple[CninfoIndustryCategory, ...]) -> None:
    by_code = {row.code: row for row in rows}
    if len(by_code) != len(rows) or "0" not in by_code or by_code["0"].parent_code != "008":
        raise CninfoIndustryError("cninfo_industry_taxonomy_incomplete")
    complete = {"0"}
    for row in rows:
        cursor = row.code
        path: set[str] = set()
        while cursor not in complete:
            if cursor in path:
                raise CninfoIndustryError("cninfo_industry_taxonomy_cycle")
            path.add(cursor)
            parent = by_code.get(cursor)
            if parent is None:
                raise CninfoIndustryError("cninfo_industry_taxonomy_parent_missing")
            cursor = parent.parent_code
        complete.update(path)


def ParseCninfoIndustryTaxonomy(
    content: bytes, *, cancel_requested: Callable[[], bool] = lambda: False
) -> CninfoIndustryTaxonomy:
    """Revalidate the original response before using network or cached taxonomy evidence."""
    categories = []
    for record in _records(content, cancel_requested):
        _check_cancel(cancel_requested)
        categories.append(_category(record))
    rows = tuple(categories)
    _validate_category_tree(rows)
    _check_cancel(cancel_requested)
    return CninfoIndustryTaxonomy(rows, hashlib.sha256(content).hexdigest(), content)


def ParseCninfoIndustryChanges(
    content: bytes,
    code: str,
    start_date: date,
    end_date: date,
    *,
    cancel_requested: Callable[[], bool] = lambda: False,
) -> CninfoIndustryChanges:
    """Preserve each native standard; an old classification is never relabelled as current."""
    _validate_query(code, start_date, end_date)
    changes = []
    for record in _records(content, cancel_requested):
        _check_cancel(cancel_requested)
        changes.append(_change(record, code, start_date, end_date))
    rows = tuple(changes)
    identities = {(row.standard_code, row.standard_name, row.changed_on) for row in rows}
    if len(identities) != len(rows):
        raise CninfoIndustryError("cninfo_industry_change_conflict")
    latest_standards = [(row.standard_code, row.standard_name) for row in rows if row.is_latest]
    if len(set(latest_standards)) != len(latest_standards):
        raise CninfoIndustryError("cninfo_industry_latest_conflict")
    _check_cancel(cancel_requested)
    return CninfoIndustryChanges(
        tuple(sorted(rows, key=lambda row: (row.changed_on, row.standard_code, row.standard_name))),
        hashlib.sha256(content).hexdigest(),
        content,
    )


def ResolveCninfoIndustryCategory(
    taxonomy: CninfoIndustryTaxonomy, change: CninfoIndustryChange
) -> CninfoIndustryCategory | None:
    """Resolve a native leaf to its declared major category, without cross-standard guessing."""
    if not change.is_current_standard:
        return None
    by_code = {row.code: row for row in taxonomy.rows}
    cursor = change.industry_code
    visited: set[str] = set()
    while cursor not in visited:
        visited.add(cursor)
        category = by_code.get(cursor)
        if category is None:
            raise CninfoIndustryError("cninfo_industry_category_missing")
        if re.fullmatch(r"[A-S][0-9]{2}", category.code):
            return category
        if category.code == "0":
            break
        cursor = category.parent_code
    raise CninfoIndustryError("cninfo_industry_category_unresolved")


def _validate_query(code: str, start_date: date, end_date: date) -> None:
    if (
        re.fullmatch(r"[0-9]{6}", code) is None
        or type(start_date) is not date or type(end_date) is not date or start_date > end_date
    ):
        raise ValueError("cninfo_industry_query_invalid")


class CninfoIndustryClient:
    """Borrow one injected transport; every call makes at most one physical request."""

    def __init__(
        self,
        transport: HttpTransport,
        *,
        request_headers: Callable[[], tuple[tuple[str, str], ...]],
        timeout_seconds: float = 15.0,
        monotonic_clock: Callable[[], float] = monotonic,
    ) -> None:
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 45:
            raise ValueError("cninfo_industry_timeout_invalid")
        self._transport = transport
        self._request_headers = request_headers
        self._timeout_seconds = timeout_seconds
        self._monotonic = monotonic_clock

    def fetch_taxonomy(
        self,
        *,
        deadline: float | None = None,
        cancel_requested: Callable[[], bool] = lambda: False,
    ) -> CninfoIndustryTaxonomy:
        content, limit = self._request(
            "p_public0002",
            {"indcode": "", "indtype": _STANDARD_CODE, "format": "json"},
            deadline,
            cancel_requested,
        )
        return ParseCninfoIndustryTaxonomy(content, cancel_requested=lambda: self._parse_cancel(limit, cancel_requested))

    def fetch_changes(
        self,
        code: str,
        start_date: date,
        end_date: date,
        *,
        deadline: float | None = None,
        cancel_requested: Callable[[], bool] = lambda: False,
    ) -> CninfoIndustryChanges:
        _validate_query(code, start_date, end_date)
        content, limit = self._request(
            "p_stock2110",
            {"scode": code, "sdate": start_date.isoformat(), "edate": end_date.isoformat()},
            deadline,
            cancel_requested,
        )
        return ParseCninfoIndustryChanges(
            content,
            code,
            start_date,
            end_date,
            cancel_requested=lambda: self._parse_cancel(limit, cancel_requested),
        )

    def _parse_cancel(self, deadline: float, cancel_requested: Callable[[], bool]) -> bool:
        self._check(deadline, cancel_requested)
        return False

    def _check(self, deadline: float, cancel_requested: Callable[[], bool]) -> None:
        if cancel_requested():
            raise HttpCancelled("cninfo_industry_cancelled")
        if self._monotonic() >= deadline:
            raise HttpTimeout("cninfo_industry_deadline_exceeded")

    def _request(
        self,
        endpoint: str,
        params: dict[str, HttpParameter],
        deadline: float | None,
        cancel_requested: Callable[[], bool],
    ) -> tuple[bytes, float]:
        if deadline is not None and not math.isfinite(deadline):
            raise ValueError("cninfo_industry_deadline_invalid")
        limit = min(deadline, self._monotonic() + self._timeout_seconds) if deadline is not None else self._monotonic() + self._timeout_seconds
        self._check(limit, cancel_requested)
        headers = dict(self._request_headers())
        if not headers.get("Accept-Enckey"):
            raise ValueError("cninfo_industry_authorization_missing")
        self._check(limit, cancel_requested)
        operation = self._transport.get if endpoint == "p_public0002" else self._transport.post
        response = operation(
            _BASE_URL + endpoint,
            timeout=max(0.001, min(self._timeout_seconds, limit - self._monotonic())),
            params=params,
            headers=headers,
            deadline=limit,
            max_response_bytes=_MAX_BYTES,
            cancel_requested=cancel_requested,
        )
        response.raise_for_status()
        self._check(limit, cancel_requested)
        content = response.content
        return content, limit


__all__ = [
    "CninfoIndustryCategory",
    "CninfoIndustryChange",
    "CninfoIndustryChanges",
    "CninfoIndustryClient",
    "CninfoIndustryError",
    "CninfoIndustryTaxonomy",
    "ParseCninfoIndustryChanges",
    "ParseCninfoIndustryTaxonomy",
    "ResolveCninfoIndustryCategory",
]
