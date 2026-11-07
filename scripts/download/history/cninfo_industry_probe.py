"""Compare explicit CNINFO query encodings outside product data, retaining original replies."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from time import monotonic
from zoneinfo import ZoneInfo

from scripts.core.paths import ExternalPath
from trader.download.infra.cninfo_headers import CninfoIndustryHeaders
from trader.infra.atomic_files.json import AtomicWriteJson
from trader.infra.http_contracts import HttpCancelled, HttpError, HttpResponse, HttpTimeout, HttpTransport
from trader.infra.process_lock import ProcessLock
from trader.infra.regular_file import ReadRegularFile

_URL = "https://webapi.cninfo.com.cn/api/stock/p_stock2110"
_MAX_BYTES = 2 * 1024 * 1024
_MAX_ROWS = 20_000
_SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class CninfoIndustryProbeRequest:
    codes: tuple[str, ...]
    from_date: date
    to_date: date
    work_root: Path
    timeout_seconds: float = 15.0
    budget_seconds: float = 180.0


@dataclass(frozen=True)
class _ParsedReply:
    resultcode: int | None = None
    count: int | None = None
    total: int | None = None
    row_count: int = 0
    actual_codes: tuple[str, ...] = ()
    records: tuple[str, ...] = ()
    null_fields: int = 0
    empty_fields: int = 0
    truncated: bool = False
    failure: str | None = None


@dataclass(frozen=True)
class _QueryResult:
    label: str
    codes: tuple[str, ...]
    http_status: int | None
    received_at: datetime | None
    response_sha256: str | None
    response_bytes: int
    reply: _ParsedReply | None
    failure: str | None
    elapsed_seconds: float

    @property
    def is_valid(self) -> bool:
        return self.failure is None and self.reply is not None and self.reply.failure is None


@dataclass
class _ProbeRun:
    request: CninfoIndustryProbeRequest
    root: Path
    transport: HttpTransport
    clock: Callable[[], datetime]
    monotonic_clock: Callable[[], float]
    cancel_requested: Callable[[], bool]
    deadline: float
    network_requests: int = 0

    def check(self) -> None:
        if self.cancel_requested():
            raise HttpCancelled("cninfo_probe_cancelled")
        if self.monotonic_clock() >= self.deadline:
            raise HttpTimeout("cninfo_probe_budget_exhausted")

    def query(self, label: str, codes: tuple[str, ...], encoding: str) -> _QueryResult:
        started = self.monotonic_clock()
        response: HttpResponse | None = None
        received_at = None
        digest = None
        reply = None
        failure = None
        try:
            self.check()
            headers = dict(CninfoIndustryHeaders(self.clock()))
            self.check()
            timeout = min(self.request.timeout_seconds, self.deadline - self.monotonic_clock())
            parameters = {
                "scode": ",".join(codes), "sdate": self.request.from_date.isoformat(),
                "edate": self.request.to_date.isoformat(),
            }
            self.network_requests += 1
            if encoding == "get_query":
                response = self.transport.get(
                    _URL, timeout=timeout, params=parameters, headers=headers, deadline=self.deadline,
                    max_response_bytes=_MAX_BYTES, cancel_requested=self.cancel_requested,
                )
            elif encoding == "post_query":
                response = self.transport.post(
                    _URL, timeout=timeout, params=parameters, headers=headers, deadline=self.deadline,
                    max_response_bytes=_MAX_BYTES, cancel_requested=self.cancel_requested,
                )
            else:
                response = self.transport.post(
                    _URL, timeout=timeout, data=parameters, headers=headers, deadline=self.deadline,
                    max_response_bytes=_MAX_BYTES, cancel_requested=self.cancel_requested,
                )
            received_at = _aware(self.clock())
            self.check()
            if not response.content or len(response.content) > _MAX_BYTES:
                raise ValueError("cninfo_probe_response_size_invalid")
            digest = hashlib.sha256(response.content).hexdigest()
            _save_reply(self.root, digest, response.content)
            AtomicWriteJson(self.root / (label + ".receipt.json"), {
                "url": _URL, "encoding": encoding, "codes": codes,
                "from_date": self.request.from_date.isoformat(), "to_date": self.request.to_date.isoformat(),
                "received_at": received_at.isoformat(), "sha256": digest, "http_status": response.status_code,
            })
            reply = _parse_reply(response.content, codes, self.request.from_date, self.request.to_date, self.check)
            response.raise_for_status()
            self.check()
        except (HttpError, OSError, ValueError) as exc:
            failure = str(exc) if re.fullmatch(r"cninfo_probe_[a-z_]+", str(exc)) else type(exc).__name__
        return _QueryResult(
            label, codes, response.status_code if response else None, received_at, digest,
            len(response.content) if response else 0, reply, failure, max(0.0, self.monotonic_clock() - started),
        )


def _aware(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("cninfo_probe_clock_invalid")
    return value.astimezone(_SHANGHAI)


def _save_reply(root: Path, digest: str, content: bytes) -> None:
    path = root / (digest + ".blob")
    try:
        with path.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        if ReadRegularFile(path, maximum_bytes=_MAX_BYTES) != content:
            raise ValueError("cninfo_probe_original_conflict") from None


def _integer(value: object) -> int | None:
    return value if type(value) is int else None


def _parse_reply(
    content: bytes,
    codes: tuple[str, ...],
    from_date: date,
    to_date: date,
    check: Callable[[], None],
) -> _ParsedReply:
    try:
        payload: object = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _ParsedReply(failure="invalid_json")
    if not isinstance(payload, dict):
        return _ParsedReply(failure="invalid_envelope")
    resultcode, count, total = (_integer(payload.get(field)) for field in ("resultcode", "count", "total"))
    records = payload.get("records")
    failure = None
    if resultcode != 200:
        failure = "query_rejected"
    elif not isinstance(records, list) or count is None or count != len(records) or count < 0:
        failure = "count_mismatch"
    elif "total" in payload and (total is None or total < count):
        failure = "total_invalid"
    if failure is not None or not isinstance(records, list):
        return _ParsedReply(resultcode, count, total, failure=failure)
    truncated = len(records) >= _MAX_ROWS or total is not None and total > len(records)
    if truncated:
        return _ParsedReply(resultcode, count, total, len(records), truncated=True, failure="truncated")
    actual_codes: set[str] = set()
    canonical: list[str] = []
    null_fields = empty_fields = 0
    for record in records:
        check()
        if not isinstance(record, dict) or any(not isinstance(key, str) for key in record):
            return _ParsedReply(resultcode, count, total, len(records), failure="record_invalid")
        code, changed_on = record.get("SECCODE"), record.get("VARYDATE")
        if not isinstance(code, str) or re.fullmatch(r"[0-9]{6}", code) is None or code not in codes:
            return _ParsedReply(resultcode, count, total, len(records), failure="code_identity_invalid")
        try:
            if not isinstance(changed_on, str) or not from_date <= date.fromisoformat(changed_on) <= to_date:
                return _ParsedReply(resultcode, count, total, len(records), failure="date_identity_invalid")
            canonical.append(json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
        except ValueError:
            return _ParsedReply(resultcode, count, total, len(records), failure="record_invalid")
        actual_codes.add(code)
        null_fields += sum(value is None for value in record.values())
        empty_fields += sum(value == "" for value in record.values())
    return _ParsedReply(resultcode, count, total, len(records), tuple(sorted(actual_codes)), tuple(sorted(canonical)), null_fields, empty_fields)


def _project(result: _QueryResult, baseline: tuple[str, ...] | None) -> dict[str, object]:
    reply = result.reply
    matches = reply.records == baseline if result.is_valid and reply is not None and baseline is not None else None
    return {
        "query": result.label, "requested_codes": result.codes, "http_status": result.http_status,
        "received_at": result.received_at.isoformat() if result.received_at else None,
        "response_sha256": result.response_sha256, "response_bytes": result.response_bytes,
        "elapsed_seconds": result.elapsed_seconds,
        "resultcode": reply.resultcode if reply else None, "count": reply.count if reply else None,
        "total": reply.total if reply else None, "row_count": reply.row_count if reply else 0,
        "actual_codes": reply.actual_codes if reply else (),
        "missing_codes": tuple(code for code in result.codes if code not in reply.actual_codes) if result.is_valid and reply is not None else (),
        "null_fields": reply.null_fields if reply else 0, "empty_string_fields": reply.empty_fields if reply else 0,
        "truncated": reply.truncated if reply else False,
        "empty": reply.row_count == 0 if result.is_valid and reply is not None else None,
        "status": "failed" if not result.is_valid else "empty" if reply is not None and not reply.records else "parsed",
        "failure": result.failure or (reply.failure if reply else None),
        "baseline_comparison": "not_compared" if matches is None else "equal" if matches else "different",
        "complete_against_baseline": matches is True and bool(baseline),
    }


def ProbeCninfoIndustryQueries(
    request: CninfoIndustryProbeRequest,
    transport: HttpTransport,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(_SHANGHAI),
    monotonic_clock: Callable[[], float] = monotonic,
    cancel_requested: Callable[[], bool] = lambda: False,
) -> dict[str, object]:
    """Compare full record multisets; HTTP success alone never establishes batch completeness."""
    if (
        not 2 <= len(request.codes) <= 50 or request.codes != tuple(sorted(set(request.codes)))
        or any(re.fullmatch(r"[0-9]{6}", code) is None for code in request.codes)
        or type(request.from_date) is not date or type(request.to_date) is not date
        or request.from_date > request.to_date or request.to_date > _aware(clock()).date()
        or not math.isfinite(request.timeout_seconds) or not 1 <= request.timeout_seconds <= 45
        or not math.isfinite(request.budget_seconds) or not 1 <= request.budget_seconds <= 7200
    ):
        raise ValueError("cninfo_probe_request_invalid")
    root = ExternalPath(request.work_root, "--industry-work-root")
    if root is None:
        raise ValueError("cninfo_probe_work_root_required")
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise ValueError("cninfo_probe_requires_empty_directory")
    started = monotonic_clock()
    run = _ProbeRun(request, root, transport, clock, monotonic_clock, cancel_requested, started + request.budget_seconds)
    batches: list[_QueryResult] = []
    singles: list[_QueryResult] = []
    with ProcessLock(root / ".cninfo-probe.lock", label="cninfo-industry-probe"):
        for encoding in ("get_query", "post_query", "post_form"):
            batches.append(run.query(encoding, request.codes, encoding))
        if len(request.codes) <= 5:
            for code in request.codes:
                singles.append(run.query("single_" + code, (code,), "post_query"))
    baseline = None
    if singles and len(singles) == len(request.codes) and all(item.is_valid for item in singles):
        baseline = tuple(sorted(record for item in singles if item.reply is not None for record in item.reply.records))
    projected_batches = [_project(item, baseline) for item in batches]
    failed = any(not item.is_valid for item in (*batches, *singles)) or any(
        item["baseline_comparison"] == "different" for item in projected_batches
    )
    if baseline == ():
        failed = True
    return {
        "status": "failed" if failed else "degraded",
        "summary": {
            "scope": "cninfo_batch_query_candidates", "production_eligible": False,
            "window_coverage_verified": False, "source_data_writes": 0,
            "network_requests": run.network_requests, "requested_codes": request.codes,
            "elapsed_seconds": max(0.0, monotonic_clock() - started),
            "requested_from": request.from_date.isoformat(), "requested_to": request.to_date.isoformat(),
            "baseline_status": "not_compared" if len(request.codes) > 5 else "empty" if baseline == () else "compared" if baseline is not None else "failed",
            "batch_queries": projected_batches, "single_queries": [_project(item, None) for item in singles],
        },
    }


__all__ = [
    "CninfoIndustryProbeRequest",
    "ProbeCninfoIndustryQueries",
]
