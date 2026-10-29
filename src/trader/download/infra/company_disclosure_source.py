"""Bounded public CNInfo market/date pages; no per-stock annual query fallback."""

from __future__ import annotations

import hashlib
import html
import re
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime
from functools import partial
from typing import Literal
from zoneinfo import ZoneInfo

from trader.download.domain.company_evidence import (
    CompanyDocumentExtraction,
    CompanyDocumentFailure,
    CompanyDocumentLimits,
    CompanyDocumentPolicies,
    CompanySourceFailure,
    DocumentRoute,
    Disclosure,
    DisclosurePage,
    DisclosurePartition,
    DisclosureQuery,
    DisclosureSecurity,
)
from trader.download.infra.company_document_ocr import CompanyOcrEngine
from trader.download.infra.company_http import CheckCompanyResponse
from trader.infra.http_contracts import (
    HttpCancelled,
    HttpConnectionError,
    HttpResponse,
    HttpResponseTooLarge,
    HttpTimeout,
    HttpTransport,
)

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_DEFAULT_DOCUMENT_LIMITS = CompanyDocumentLimits()
_DEFAULT_EXTENDED_LIMITS = CompanyDocumentLimits(64 * 1024 * 1024, 1000, 2_000_000)
_DEFAULT_POLICIES = CompanyDocumentPolicies("text_3:default", "ocr_1:unavailable", "limits_1:default")


class CninfoMarketDisclosureSource:
    def __init__(
        self,
        session: HttpTransport,
        *,
        monotonic: Callable[[], float],
        sleeper: Callable[[float], None],
        timeout_seconds: float = 8.0,
        document_parse_timeout_seconds: float = 10.0,
        request_interval_seconds: float = 1.0,
        document_limits: CompanyDocumentLimits = _DEFAULT_DOCUMENT_LIMITS,
        extended_document_limits: CompanyDocumentLimits = _DEFAULT_EXTENDED_LIMITS,
        document_policies: CompanyDocumentPolicies = _DEFAULT_POLICIES,
        ocr_engine: CompanyOcrEngine | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> None:
        self._session = session
        self._monotonic = monotonic
        self._sleep = sleeper
        self._timeout = timeout_seconds
        self._document_parse_timeout = document_parse_timeout_seconds
        self._document_limits = document_limits
        self._extended_document_limits = extended_document_limits
        self._document_policies = document_policies
        self._ocr_engine = ocr_engine
        self._request_interval = request_interval_seconds
        self._cancel_requested = cancel_requested
        self._next_request = 0.0
        self._securities: tuple[DisclosureSecurity, ...] | None = None

    def fetch(self, partition: DisclosurePartition, *, order: Literal["", "asc", "desc"] = "") -> DisclosurePage:
        if partition.page > 100:
            raise ValueError("cninfo_page_limit")
        response = self._request(partial(self._fetch_page, partition, order=order))
        return _parse_page(response.json(), partition)

    def _fetch_page(self, partition: DisclosurePartition, *, order: str = "") -> HttpResponse:
        return self._session.post(
            "https://www.cninfo.com.cn/new/hisAnnouncement/query",
            data={
                "pageNum": str(partition.page),
                "pageSize": str(partition.page_size),
                "column": partition.market,
                "plate": partition.query.plate
                if partition.query
                else {"sse": "sh", "szse": "sz", "bse": "bj"}[partition.market],
                "tabName": "fulltext",
                "stock": ";".join(f"{s.code},{s.org_id}" for s in partition.query.securities)
                if partition.query
                else "",
                "searchkey": "",
                "secid": "",
                "category": "",
                "trade": "",
                "seDate": f"{partition.start.isoformat()}~{partition.end.isoformat()}",
                "sortName": "code" if order else "",
                "sortType": order,
                "isHLtitle": "false",
            },
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.cninfo.com.cn/"},
            timeout=self._timeout,
            deadline=self._monotonic() + self._timeout,
            cancel_requested=self._cancel_requested,
        )

    def split_queries(self, partition: DisclosurePartition) -> tuple[DisclosureQuery, ...]:
        query = partition.query
        if query is None:
            return tuple(
                DisclosureQuery(plate)
                for plate in {"sse": ("shmb", "shkcp"), "szse": ("szmb", "szcy"), "bse": ("bj",)}[partition.market]
            )
        if query.securities:
            if len(query.securities) == 1:
                raise ValueError("cninfo_query_unsplittable")
            middle = len(query.securities) // 2
            return (
                DisclosureQuery(query.plate, query.securities[:middle]),
                DisclosureQuery(query.plate, query.securities[middle:]),
            )
        if self._securities is None:
            response = self._request(
                lambda: self._session.get(
                    "https://www.cninfo.com.cn/new/data/szse_stock.json",
                    timeout=self._timeout,
                    deadline=self._monotonic() + self._timeout,
                    cancel_requested=self._cancel_requested,
                ),
            )
            payload = response.json()
            rows = payload.get("stockList") if isinstance(payload, Mapping) else None
            if not isinstance(rows, list) or not rows:
                raise ValueError("cninfo_security_list_invalid")
            parsed = []
            for row in rows:
                if (
                    not isinstance(row, Mapping)
                    or not isinstance(row.get("code"), str)
                    or not isinstance(row.get("orgId"), str)
                ):
                    raise ValueError("cninfo_security_list_invalid")
                parsed.append(DisclosureSecurity(row["code"], row["orgId"]))
            if len({s.code for s in parsed}) != len(parsed):
                raise ValueError("cninfo_security_list_invalid")
            self._securities = tuple(sorted(parsed, key=lambda s: s.code))
        members = tuple(s for s in self._securities if _plate(s.code) == query.plate)
        if not members:
            raise ValueError("cninfo_security_list_incomplete")
        return tuple(DisclosureQuery(query.plate, members[i : i + 32]) for i in range(0, len(members), 32))

    def repair_queries(
        self,
        partition: DisclosurePartition,
        members: tuple[DisclosureSecurity, ...],
        *,
        missing_rows: int,
        report: Callable[[DisclosurePartition], None] | None = None,
    ) -> tuple[DisclosureQuery, ...]:
        """Discover omitted historical issuers without discarding completed queries.

        The current autocomplete list is not a historical issuer universe. Bounded
        code-sorted parent samples discover absent issuers; only a later exact
        record-union check can declare coverage, even if discovery is incomplete.
        """
        known = {member.org_id for member in members}
        found: list[DisclosureSecurity] = []
        records: set[tuple[str, tuple[str, ...]]] = set()
        for plate in sorted({_plate(member.code) for member in members}):
            orders = ["asc", "desc"]
            for number in range(1, 101):
                for order in tuple(orders):
                    sample = replace(partition, query=DisclosureQuery(plate), page=number)
                    response = self._request(partial(self._fetch_page, sample, order=order))
                    payload = response.json()
                    page = _parse_page(payload, sample)
                    if report is not None:
                        report(replace(sample, expected_rows=page.total))
                    assert isinstance(payload, Mapping)  # The page parser has validated the envelope.
                    rows = payload.get("announcements") or []
                    for raw, item in zip(rows, page.rows, strict=True):
                        issuer = str(raw.get("orgId") or "")
                        if issuer and issuer not in known:
                            found.append(DisclosureSecurity(item.codes[0], issuer))
                            known.add(issuer)
                        if any(member.org_id == issuer for member in found):
                            records.add((item.identity, item.codes))
                    if len(records) >= missing_rows:
                        return _repair_groups(found)
                    if not page.has_more:
                        orders.remove(order)
                if not orders:
                    break
        return _repair_groups(found)

    def _request(self, send: Callable[[], HttpResponse]) -> HttpResponse:
        # Only the read-only CNInfo protocol owns retries; the shared transport remains single-attempt.
        for attempt in range(3):
            self._pace()
            try:
                response = send()
                CheckCompanyResponse(response)
                self._check_cancelled()
                return response
            except (HttpTimeout, HttpConnectionError, CompanySourceFailure) as exc:
                if isinstance(exc, CompanySourceFailure) and exc.status_code not in {408, 500, 502, 503, 504}:
                    raise
                self._check_cancelled()
                if attempt == 2:
                    raise
                self._next_request = max(self._next_request, self._monotonic() + 2 ** (attempt + 1))
        raise AssertionError("bounded CNInfo attempts must return or raise")

    def _check_cancelled(self) -> None:
        if self._cancel_requested is not None and self._cancel_requested():
            raise HttpCancelled("http_cancelled")

    def _pace(self) -> None:
        self._check_cancelled()
        delay = max(0.0, self._next_request - self._monotonic())
        while delay > 0:
            interval = min(0.25, delay)
            self._sleep(interval)
            self._check_cancelled()
            delay -= interval
        self._next_request = self._monotonic() + self._request_interval

    def document(self, item: Disclosure, *, route: DocumentRoute = "standard") -> CompanyDocumentExtraction:
        if not item.url.startswith("https://static.cninfo.com.cn/finalpage/"):
            raise CompanyDocumentFailure("disclosure_document_unavailable")
        limits = self._extended_document_limits if route == "limits" else self._document_limits
        try:
            response = self._download_document(item, limits.max_bytes)
        except HttpResponseTooLarge as exc:
            if route == "limits":
                raise CompanyDocumentFailure("disclosure_document_oversized") from exc
            try:
                response = self._download_document(item, self._extended_document_limits.max_bytes)
            except HttpResponseTooLarge as expanded:
                raise CompanyDocumentFailure("disclosure_document_oversized") from expanded
            limits = self._extended_document_limits
            route = "limits"
        payload = response.content
        payload_hash = hashlib.sha256(payload).digest()
        if route == "ocr":
            return self._ocr(payload, payload_hash)
        try:
            text = self._parse_document(payload, limits)
        except CompanyDocumentFailure as exc:
            if exc.reason in {"disclosure_document_no_text", "disclosure_document_short_text", "disclosure_document_unreadable"}:
                return self._ocr(payload, payload_hash)
            if route == "standard" and exc.reason in {
                "disclosure_document_page_limit",
                "disclosure_document_text_limit",
            }:
                try:
                    text = self._parse_document(payload, self._extended_document_limits)
                except CompanyDocumentFailure as expanded:
                    if expanded.reason in {
                        "disclosure_document_no_text",
                        "disclosure_document_short_text",
                        "disclosure_document_unreadable",
                    }:
                        return self._ocr(payload, payload_hash)
                    raise
                route = "limits"
            else:
                raise
        return CompanyDocumentExtraction(text, "text", payload_hash, self._document_policies.for_route(route))

    def _download_document(self, item: Disclosure, max_bytes: int) -> HttpResponse:
        return self._request(
            lambda: self._session.get(
                item.url,
                timeout=self._timeout,
                deadline=self._monotonic() + self._timeout,
                max_response_bytes=max_bytes,
                cancel_requested=self._cancel_requested,
            ),
        )

    def _ocr(self, payload: bytes, payload_hash: bytes) -> CompanyDocumentExtraction:
        if self._ocr_engine is None:
            raise CompanyDocumentFailure("disclosure_document_ocr_unavailable", payload_hash=payload_hash)
        text, quality = self._ocr_engine.extract(payload, payload_hash)
        return CompanyDocumentExtraction(text, "ocr", payload_hash, self._document_policies.ocr, quality)

    def _parse_document(self, payload: bytes, limits: CompanyDocumentLimits | None = None) -> str:
        limits = limits or self._document_limits
        payload_hash = hashlib.sha256(payload).digest()
        self._check_cancelled()
        parse_deadline = self._monotonic() + self._document_parse_timeout
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "trader.download.infra.company_document_parser",
                "--max-bytes",
                str(limits.max_bytes),
                "--max-pages",
                str(limits.max_pages),
                "--max-text-characters",
                str(limits.max_text_characters),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        pending_input: bytes | None = payload
        try:
            while True:
                self._check_cancelled()
                remaining = parse_deadline - self._monotonic()
                if remaining <= 0:
                    raise CompanyDocumentFailure("disclosure_document_parse_timeout", payload_hash=payload_hash)
                try:
                    output, _ = process.communicate(input=pending_input, timeout=min(0.1, remaining))
                    break
                except subprocess.TimeoutExpired:
                    # communicate retains partial writes and reads after a timeout.
                    pending_input = None
            self._check_cancelled()
        finally:
            _close_parser(process)
        if process.returncode:
            reason = {
                3: "encrypted",
                4: "page_limit",
                5: "expanded_page_limit",
                6: "text_limit",
                7: "no_text",
                8: "short_text",
            }
            raise CompanyDocumentFailure(
                f"disclosure_document_{reason.get(process.returncode, 'unreadable')}", payload_hash=payload_hash
            )
        return output.decode("utf-8")


def _close_parser(process: subprocess.Popen[bytes]) -> None:
    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1.0)
    finally:
        if process.stdin is not None:
            process.stdin.close()
        if process.stdout is not None:
            process.stdout.close()


def _plate(code: str) -> str:
    # CNInfo's main-board announcement totals include Shanghai B-share issuers.
    # Keep those rows for exact coverage; publication still uses its A-share population.
    if code.startswith(("6", "900")):
        return "shkcp" if code.startswith(("688", "689")) else "shmb"
    if code.startswith(("4", "8", "9")):
        return "bj"
    return "szcy" if code.startswith(("300", "301")) else "szmb"


def _repair_groups(members: list[DisclosureSecurity]) -> tuple[DisclosureQuery, ...]:
    queries: list[DisclosureQuery] = []
    for plate in sorted({_plate(member.code) for member in members}):
        group = tuple(member for member in members if _plate(member.code) == plate)
        queries.extend(DisclosureQuery(plate, group[start : start + 32]) for start in range(0, len(group), 32))
    return tuple(queries)


def _parse_page(payload: object, partition: DisclosurePartition) -> DisclosurePage:
    if not isinstance(payload, Mapping):
        raise ValueError("cninfo_schema_invalid")
    total, more = payload.get("totalRecordNum"), payload.get("hasMore")
    rows = payload.get("announcements")
    if rows is None and total == 0:
        rows = []
    if type(total) is not int or total < 0 or type(more) is not bool or not isinstance(rows, list):
        raise ValueError("cninfo_pagination_invalid")
    result: list[Disclosure] = []
    query = partition.query
    selected = {security.code for security in query.securities} if query else set()
    issuers = {security.org_id for security in query.securities} if query else set()
    for raw in rows:
        item = _parse_disclosure(raw, partition)
        if query is not None:
            assert isinstance(raw, Mapping)
            if (selected and not selected.intersection(item.codes) and str(raw.get("orgId") or "") not in issuers) or (
                not selected and any(_plate(code) != query.plate for code in item.codes)
            ):
                raise ValueError("cninfo_query_filter_ignored")
        result.append(item)
    if more and not result:
        raise ValueError("cninfo_empty_nonterminal_page")
    return DisclosurePage(tuple(result), total, more)


def _parse_disclosure(row: object, partition: DisclosurePartition) -> Disclosure:
    if not isinstance(row, Mapping):
        raise ValueError("cninfo_row_invalid")
    identity = str(row.get("announcementId") or "")
    codes = tuple(sorted(set(str(row.get("secCode") or "").split(","))))
    title = html.unescape(re.sub(r"<[^>]+>", "", str(row.get("announcementTitle") or ""))).strip()
    stamp = row.get("announcementTime")
    adjunct = str(row.get("adjunctUrl") or "")
    if (
        not identity
        or not title
        or not isinstance(stamp, (int, float))
        or any(len(code) != 6 or not code.isdigit() for code in codes)
    ):
        raise ValueError("cninfo_identity_invalid")
    published = datetime.fromtimestamp(stamp / 1000, _SHANGHAI)
    if not partition.start <= published.date() <= partition.end:
        raise ValueError("cninfo_date_filter_ignored")
    if adjunct and (not adjunct.startswith("finalpage/") or ".." in adjunct):
        raise ValueError("cninfo_document_path_invalid")
    digest = hashlib.sha256(
        f"{identity}\n{title}\n{published.isoformat()}\n{adjunct}\n{row.get('adjunctSize')}\n{row.get('adjunctType')}".encode()
    ).hexdigest()
    return Disclosure(
        "cninfo",
        identity,
        codes,
        title,
        published,
        f"https://static.cninfo.com.cn/{adjunct}" if adjunct else "",
        digest,
    )


__all__ = ["CninfoMarketDisclosureSource"]
