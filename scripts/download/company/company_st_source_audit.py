"""Read-only, bounded name-history comparisons; never qualify production ST records."""

from __future__ import annotations

import math
import json
import hashlib
import re
import time
import unicodedata
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import date, datetime
from html.parser import HTMLParser
from typing import Literal, cast
from zoneinfo import ZoneInfo

from trader.download.domain.security_eligibility import IsStSecurityName
from trader.download.infra.history_st_source import ParseNameHistorySt
from trader.download.infra.history_sync_failure import HistoryFailureDetail
from trader.infra.http_contracts import (
    HttpCancelled, HttpConnectionError, HttpError, HttpResponse, HttpResponseTooLarge, HttpStatusError, HttpTimeout,
    HttpTransport,
)

_Status = Literal["ever_st", "clear", "unknown"]
_Source = Literal["eastmoney_f10", "sina_name_history"]
_MAX_BYTES = 1024 * 1024
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_EMPTY = frozenset({"", "-", "--", "暂无", "暂无数据"})


@dataclass(frozen=True, slots=True)
class CompanyStSourceRequest:
    code: str
    current_name: str
    expected_status: _Status

    def __post_init__(self) -> None:
        if (
            not isinstance(self.code, str) or re.fullmatch(r"[036][0-9]{5}", self.code) is None
            or not isinstance(self.current_name, str) or not _name(self.current_name)
            or len(self.current_name) > 40 or not all(character.isprintable() for character in self.current_name)
            or not isinstance(self.expected_status, str) or self.expected_status not in {"ever_st", "clear", "unknown"}
        ):
            raise ValueError("company_st_source_request_invalid")


@dataclass(frozen=True, slots=True)
class _Job:
    source: _Source
    round: int
    request: CompanyStSourceRequest


@dataclass(frozen=True, slots=True)
class _Structure:
    format: str
    rows: int | None = None
    fields: tuple[tuple[str, str], ...] = ()
    observed_code: str | None = field(default=None, compare=False)
    observed_current_name: str | None = field(default=None, compare=False)
    observed_a_name: str | None = field(default=None, compare=False)


@dataclass(frozen=True, slots=True)
class _Decoded:
    status: Literal["decoded", "unknown_unverified"]
    eligibility: _Status
    original_parser_status: _Status | None
    current_name_matches: bool | None
    current_name_is_st: bool
    history_is_st: bool
    empty_history: bool
    former_names_excerpt: str
    former_names_sha256: str
    structure: _Structure


@dataclass(frozen=True, slots=True)
class _Observation:
    job: _Job
    attempted: bool
    status: str
    elapsed_seconds: float
    status_code: int | None = None
    response_bytes: int | None = None
    decoded: _Decoded | None = None
    reason: str = ""
    diagnostic: str = ""
    structure: _Structure = _Structure("not_observed")


class _InvalidNameHistory(ValueError):
    def __init__(self, reason: str, structure: _Structure) -> None:
        super().__init__(reason)
        self.structure = structure


class _SinaNameCell(HTMLParser):
    """Inspect only the name-history cell; production classification stays in its parser."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._cell: list[str] | None = None
        self._next = False
        self.names: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "td":
            self._cell = []
        elif self._cell is not None:
            self._cell.append(" ")

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._cell is None:
            return
        if tag.lower() != "td":
            self._cell.append(" ")
            return
        cell = _name("".join(self._cell))
        self._cell = None
        if self._next:
            self.names, self._next = cell, False
        elif self.names is None and cell.rstrip(":：") == "证券简称更名历史":
            self._next = True


def AuditCompanyStSources(
    requests: tuple[CompanyStSourceRequest, ...], *,
    session_factory: Callable[[], AbstractContextManager[HttpTransport]],
    rounds: int = 3, workers: int = 2, timeout_seconds: float = 8.0, deadline: float,
    cancel_requested: Callable[[], bool] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    clock: Callable[[], datetime] = lambda: datetime.now(_SHANGHAI),
) -> dict[str, object]:
    """One physical transport call per scheduled source sample, without hidden retries."""
    if (
        not isinstance(requests, tuple) or not 1 <= len(requests) <= 64
        or not all(isinstance(request, CompanyStSourceRequest) for request in requests)
        or len({request.code for request in requests}) != len(requests)
        or type(rounds) is not int or not 1 <= rounds <= 5
        or type(workers) is not int or not 1 <= workers <= 4
        or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 15
        or not math.isfinite(deadline)
    ):
        raise ValueError("company_st_source_audit_options_invalid")
    observed_at = clock()
    if observed_at.utcoffset() is None:
        raise ValueError("company_st_source_audit_clock_invalid")
    checked_on = observed_at.astimezone(_SHANGHAI).date()
    started = monotonic()
    jobs = tuple(
        job for request in requests for job in (
            _Job("eastmoney_f10", 1, request), _Job("sina_name_history", 1, request),
        )
    ) + tuple(_Job("eastmoney_f10", number, request) for number in range(2, rounds + 1) for request in requests)

    def stop_reason() -> str:
        if cancel_requested is not None and cancel_requested():
            return "cancelled"
        return "deadline_exhausted" if monotonic() >= deadline else ""

    def observe(job: _Job) -> _Observation:
        mark, attempted = monotonic(), False
        response: HttpResponse | None = None
        reason = stop_reason()
        if reason:
            return _Observation(job, False, "not_requested", 0.0, reason=reason)
        try:
            with session_factory() as session:
                reason = stop_reason()
                if reason:
                    return _Observation(job, False, "not_requested", max(0.0, monotonic() - mark), reason=reason)
                prefix = "SH" if job.request.code.startswith("6") else "SZ"
                url = (
                    "https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/PageAjax?code=" + prefix + job.request.code
                    if job.source == "eastmoney_f10" else
                    "https://vip.stock.finance.sina.com.cn/corp/go.php/vCI_CorpInfo/stockid/" + job.request.code + ".phtml"
                )
                attempted = True
                response = session.get(url, timeout=min(timeout_seconds, max(0.001, deadline - monotonic())),
                    deadline=deadline, max_response_bytes=_MAX_BYTES, cancel_requested=lambda: bool(stop_reason()),
                    headers={"User-Agent": "Mozilla/5.0", "Referer": "https://emweb.securities.eastmoney.com/"}
                    if job.source == "eastmoney_f10" else {"User-Agent": "Mozilla/5.0"})
                reason = stop_reason()
                if reason:
                    return _Observation(job, True, "cancelled" if reason == "cancelled" else "timed_out",
                        max(0.0, monotonic() - mark), response.status_code, len(response.content), reason=reason)
                response.raise_for_status()
                if len(response.content) > _MAX_BYTES:
                    raise HttpResponseTooLarge("http_response_too_large")
                decoded = (_eastmoney(job.request, json.loads(response.content.decode("utf-8", errors="strict"))) if job.source == "eastmoney_f10" else
                    _sina(job.request, response.content.decode("gb18030", errors="strict"), checked_on))
            reason = stop_reason()
            if reason:
                return _Observation(job, True, "cancelled" if reason == "cancelled" else "timed_out",
                    max(0.0, monotonic() - mark), response.status_code, len(response.content), reason=reason)
            return _Observation(job, True, decoded.status, max(0.0, monotonic() - mark),
                response.status_code, len(response.content), decoded=decoded, structure=decoded.structure,
                reason="empty_name_history_unverified" if decoded.empty_history else "")
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            reason = stop_reason()
            status = ("cancelled" if reason == "cancelled" else "timed_out" if reason or isinstance(exc, HttpTimeout)
                      else "cancelled" if isinstance(exc, HttpCancelled) else "failed")
            return _Observation(job, attempted, status, max(0.0, monotonic() - mark),
                response.status_code if response is not None else None,
                len(response.content) if response is not None else None,
                reason=reason or _failure_reason(exc),
                diagnostic=HistoryFailureDetail(exc),
                structure=exc.structure if isinstance(exc, _InvalidNameHistory) else _Structure("not_decoded"))

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="st-source-audit") as executor:
        observations = tuple(executor.map(observe, jobs))
    stopped = stop_reason()
    comparisons = tuple(_compare(request, observations) for request in requests)
    inconsistent = any(_expected_match(item) is False for item in observations) or any(
        item["source_conflicts"] or item["eligibility_drift"] or item["observation_drift"] for item in comparisons
    ) or any(item.job.request.expected_status == "ever_st" and item.decoded is not None
             and not item.decoded.history_is_st for item in observations)
    status = "cancelled" if stopped == "cancelled" else "timed_out" if stopped else (
        "observed" if all(item.status == "decoded" for item in observations) and not inconsistent else "degraded"
    )
    return {
        "probe": "st_name_history", "status": status, "read_only": True, "source_data_writes": 0,
        "production_adopted": False, "field_correctness_verified": False, "full_market_verified": False,
        "observed_at": observed_at.astimezone(_SHANGHAI).isoformat(),
        "expectation_role": "local_reference_not_absolute_truth", "session_policy": "new_transport_per_request",
        "rounds": rounds, "workers": workers, "timeout_seconds": timeout_seconds,
        "max_response_bytes": _MAX_BYTES, "planned_requests": len(jobs), "request_limit": len(jobs),
        "actual_requests": sum(item.attempted for item in observations),
        "elapsed_seconds": max(0.0, monotonic() - started),
        "sources": [_source_summary(source, observations) for source in ("eastmoney_f10", "sina_name_history")],
        "observations": [_project(item) for item in observations],
        "comparisons": list(comparisons),
        "source_conflicts": sum(cast(int, item["source_conflicts"]) for item in comparisons),
        "eligibility_drift_codes": [item["code"] for item in comparisons if item["eligibility_drift"]],
        "observation_drift_codes": [item["code"] for item in comparisons if item["observation_drift"]],
    }


def _eastmoney(request: CompanyStSourceRequest, payload: object) -> _Decoded:
    rows = payload.get("jbzl") if isinstance(payload, Mapping) else None
    structure = _Structure("json", len(rows) if isinstance(rows, list) else None,
                           (("jbzl", _type_name(rows)),))
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], Mapping):
        raise _InvalidNameHistory("eastmoney_name_history_rows_invalid", structure)
    row = rows[0]
    structure = _Structure("json", 1, tuple((field, _type_name(row.get(field))) for field in (
        "SECURITY_CODE", "SECURITY_NAME_ABBR", "STR_NAMEA", "FORMERNAME",
    )), observed_code=_observed_code(row.get("SECURITY_CODE")),
        observed_current_name=_observed_name(row.get("SECURITY_NAME_ABBR")),
        observed_a_name=_observed_name(row.get("STR_NAMEA")))
    if row.get("SECURITY_CODE") != request.code:
        raise _InvalidNameHistory("eastmoney_security_code_mismatch", structure)
    current = row.get("SECURITY_NAME_ABBR")
    if not isinstance(current, str) or _name(current) != _name(request.current_name):
        raise _InvalidNameHistory("eastmoney_current_name_mismatch", structure)
    a_name = row.get("STR_NAMEA")
    if a_name not in (None, "") and (not isinstance(a_name, str) or _name(a_name) != _name(current)):
        raise _InvalidNameHistory("eastmoney_current_name_mismatch", structure)
    former = row.get("FORMERNAME")
    if not isinstance(former, str):
        raise _InvalidNameHistory("eastmoney_former_names_missing_or_invalid", structure)
    return _decoded(current, former, structure, name_matches=True)


def _sina(request: CompanyStSourceRequest, text: str, checked_on: date) -> _Decoded:
    cell = _SinaNameCell()
    cell.feed(text)
    try:
        result = ParseNameHistorySt(request.code, request.current_name, checked_on, text)
    except ValueError:
        if cell.names is not None and _name(cell.names) in _EMPTY:
            return _decoded(request.current_name, cell.names, _Structure("html_name_history", 1,
                (("name_history", "str"),)))
        raise
    if cell.names is None:
        raise _InvalidNameHistory("sina_name_history_observation_missing", _Structure("html_name_history"))
    return _decoded(request.current_name, cell.names, _Structure("html_name_history", 1,
        (("name_history", "str"),)), original_status=result.status)


def _decoded(
    current: str, former: str, structure: _Structure, *, name_matches: bool | None = None,
    original_status: _Status | None = None,
) -> _Decoded:
    former = " ".join(_name(former).split())
    empty, history_st, current_st = former in _EMPTY, IsStSecurityName(former), IsStSecurityName(current)
    eligibility: _Status = ("unknown" if empty else original_status if original_status is not None else
                            "ever_st" if history_st or current_st else "clear")
    return _Decoded("unknown_unverified" if empty else "decoded", eligibility, original_status, name_matches,
                    current_st, history_st, empty, _excerpt(former), hashlib.sha256(former.encode("utf-8")).hexdigest(), structure)


def _source_summary(source: str, observations: tuple[_Observation, ...]) -> dict[str, object]:
    items = tuple(item for item in observations if item.job.source == source)
    attempted = tuple(item for item in items if item.attempted)
    usable = tuple(item for item in attempted if item.status == "decoded")
    elapsed = sorted(item.elapsed_seconds for item in attempted)
    statuses: tuple[_Status, ...] = ("ever_st", "clear", "unknown")
    confusion = {
        expected: {observed: sum(item.job.request.expected_status == expected and _eligibility(item) == observed
            for item in attempted) for observed in statuses} for expected in statuses
    }
    return {
        "source": source, "planned_requests": len(items), "actual_requests": len(attempted),
        "http_successes": sum(item.status_code is not None and 200 <= item.status_code < 300 for item in attempted),
        "usable_results": len(usable), "success_rate": len(usable) / len(attempted) if attempted else None,
        "http_success_rate": sum(item.status_code is not None and 200 <= item.status_code < 300 for item in attempted) / len(attempted) if attempted else None,
        "unknown_results": sum(_eligibility(item) == "unknown" for item in attempted),
        "failed_requests": sum(item.status in {"failed", "timed_out", "cancelled"} for item in attempted),
        "not_requested": len(items) - len(attempted),
        "p50_seconds": _percentile(elapsed, 0.5), "p95_seconds": _percentile(elapsed, 0.95),
        "max_seconds": max(elapsed) if elapsed else None, "confusion": confusion,
        "known_positive_misses": sum(item.job.request.expected_status == "ever_st" and _eligibility(item) == "clear" for item in attempted),
        "known_positive_unknown": sum(item.job.request.expected_status == "ever_st" and _eligibility(item) == "unknown" for item in attempted),
        "historical_positive_hits": sum(item.job.request.expected_status == "ever_st" and item.decoded is not None
            and item.decoded.history_is_st for item in attempted),
        "historical_positive_unverified": sum(item.job.request.expected_status == "ever_st" and _eligibility(item) != "clear"
            and (item.decoded is None or not item.decoded.history_is_st) for item in attempted),
        "local_clear_conflicts": sum(item.job.request.expected_status == "clear" and _eligibility(item) == "ever_st" for item in attempted),
    }


def _compare(request: CompanyStSourceRequest, observations: tuple[_Observation, ...]) -> dict[str, object]:
    items = tuple(item for item in observations if item.job.request.code == request.code)
    candidate = tuple(item for item in items if item.job.source == "eastmoney_f10")
    baseline = next(item for item in items if item.job.source == "sina_name_history")
    known = tuple(item for item in candidate if _eligibility(item) != "unknown")
    conflicts = sum(_eligibility(item) != _eligibility(baseline) for item in known) if _eligibility(baseline) != "unknown" else 0
    return {
        "code": request.code, "current_name": _excerpt(request.current_name), "expected_status": request.expected_status,
        "sina_eligibility": _eligibility(baseline), "eastmoney_eligibility": [_eligibility(item) for item in candidate],
        "source_conflicts": conflicts, "eligibility_drift": len({_eligibility(item) for item in known}) > 1,
        "observation_drift": len({(item.status, _eligibility(item), item.structure,
            item.decoded.former_names_sha256 if item.decoded is not None else None,
            item.decoded.current_name_is_st if item.decoded is not None else None,
            item.decoded.history_is_st if item.decoded is not None else None) for item in candidate}) > 1,
        "expected_matches": [_expected_match(item) for item in candidate],
        "sina_expected_match": _expected_match(baseline),
        "historical_positive_found": [item.decoded.history_is_st if item.decoded is not None else None for item in candidate],
    }


def _project(item: _Observation) -> dict[str, object]:
    decoded = item.decoded
    return {
        "source": item.job.source, "code": item.job.request.code, "round": item.job.round,
        "current_name": _excerpt(item.job.request.current_name), "expected_status": item.job.request.expected_status,
        "attempted": item.attempted, "status": item.status, "eligibility": _eligibility(item),
        "expected_match": _expected_match(item), "elapsed_seconds": item.elapsed_seconds,
        "http_status": item.status_code, "response_bytes": item.response_bytes,
        "reason": item.reason, "diagnostic": item.diagnostic,
        "structure": {"format": item.structure.format, "rows": item.structure.rows, "fields": dict(item.structure.fields),
            "observed_code": item.structure.observed_code, "observed_current_name": item.structure.observed_current_name,
            "observed_a_name": item.structure.observed_a_name},
        "current_name_matches": decoded.current_name_matches if decoded is not None else None,
        "current_name_is_st": decoded.current_name_is_st if decoded is not None else None,
        "history_is_st": decoded.history_is_st if decoded is not None else None,
        "empty_history": decoded.empty_history if decoded is not None else None,
        "former_names_excerpt": decoded.former_names_excerpt if decoded is not None else "",
        "former_names_sha256": decoded.former_names_sha256 if decoded is not None else None,
        "original_parser_status": decoded.original_parser_status if decoded is not None else None,
    }


def _eligibility(item: _Observation) -> _Status:
    return item.decoded.eligibility if item.decoded is not None else "unknown"


def _expected_match(item: _Observation) -> bool | None:
    expected, actual = item.job.request.expected_status, _eligibility(item)
    if expected == actual == "ever_st" and item.decoded is not None and not item.decoded.history_is_st:
        return None
    return None if "unknown" in {expected, actual} else expected == actual


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    position = fraction * (len(values) - 1)
    lower = math.floor(position)
    return values[lower] + (values[math.ceil(position)] - values[lower]) * (position - lower)


def _name(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip()


def _excerpt(value: str) -> str:
    value = re.sub(r"(?:https?://|www\.)\S+|\S+@\S+|[0-9]{7,}", "[省略]", _name(value))
    return "".join(character for character in value if character.isprintable())[:160]


def _type_name(value: object) -> str:
    return {str: "str", int: "int", float: "float", bool: "bool", list: "list", dict: "object", type(None): "null"}.get(type(value), "other")


def _observed_code(value: object) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"[0-9]{6}", value) is not None else None


def _observed_name(value: object) -> str | None:
    if not isinstance(value, str) or not 1 <= len(_name(value)) <= 40:
        return None
    return _excerpt(value)


def _failure_reason(exc: BaseException) -> str:
    if isinstance(exc, _InvalidNameHistory):
        return str(exc)
    if isinstance(exc, HttpStatusError):
        return f"http_{exc.status_code}"
    if isinstance(exc, HttpResponseTooLarge):
        return "http_response_too_large"
    if isinstance(exc, HttpTimeout):
        return "source_timeout"
    if isinstance(exc, HttpConnectionError):
        return "source_connection_failed"
    if isinstance(exc, HttpCancelled):
        return "source_cancelled"
    if isinstance(exc, HttpError):
        return "source_http_failed"
    if isinstance(exc, UnicodeDecodeError):
        return "source_text_encoding_invalid"
    if isinstance(exc, json.JSONDecodeError):
        return "source_json_invalid"
    if isinstance(exc, ValueError) and str(exc) in {"history_st_name_history_missing", "history_st_name_history_empty"}:
        return str(exc)
    return "source_io_failed" if isinstance(exc, OSError) else "source_validation_failed"


__all__ = ["AuditCompanyStSources", "CompanyStSourceRequest"]
