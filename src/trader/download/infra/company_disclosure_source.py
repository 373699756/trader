"""Bounded public CNInfo market/date pages; no per-stock annual query fallback."""

from __future__ import annotations

import hashlib
import html
import re
import subprocess
import sys
from collections.abc import Callable, Mapping
from datetime import datetime
from zoneinfo import ZoneInfo

from trader.download.domain.company_evidence import (
    CompanyDocumentLimits,
    CompanySourceFailure,
    Disclosure,
    DisclosurePage,
    DisclosurePartition,
    DisclosureQuery,
    DisclosureSecurity,
)
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
        cancel_requested: Callable[[], bool] | None = None,
    ) -> None:
        self._session = session
        self._monotonic = monotonic
        self._sleep = sleeper
        self._timeout = timeout_seconds
        self._document_parse_timeout = document_parse_timeout_seconds
        self._document_limits = document_limits
        self._request_interval = request_interval_seconds
        self._cancel_requested = cancel_requested
        self._next_request = 0.0
        self._securities: tuple[DisclosureSecurity, ...] | None = None

    def fetch(self, partition: DisclosurePartition, *, deadline: float) -> DisclosurePage:
        if partition.page > 100:
            raise ValueError("cninfo_page_limit")
        response = self._request(lambda: self._fetch_page(partition, deadline), deadline)
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise ValueError("cninfo_schema_invalid")
        total, more = payload.get("totalRecordNum"), payload.get("hasMore")
        rows = payload.get("announcements")
        if rows is None and total == 0:
            rows = []
        if type(total) is not int or total < 0 or type(more) is not bool or not isinstance(rows, list):
            raise ValueError("cninfo_pagination_invalid")
        result: list[Disclosure] = []
        for row in rows:
            result.append(_parse_disclosure(row, partition))
        query = partition.query
        if query is not None:
            selected = {security.code for security in query.securities}
            for item in result:
                if (selected and not selected.intersection(item.codes)) or (
                    not selected and any(_plate(code) != query.plate for code in item.codes)
                ):
                    raise ValueError("cninfo_query_filter_ignored")
        if more and not result:
            raise ValueError("cninfo_empty_nonterminal_page")
        return DisclosurePage(tuple(result), total, more)

    def _fetch_page(self, partition: DisclosurePartition, deadline: float) -> HttpResponse:
        return self._session.post(
            "https://www.cninfo.com.cn/new/hisAnnouncement/query",
            data={
                "pageNum": str(partition.page),
                "pageSize": "30",
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
                "sortName": "",
                "sortType": "",
                "isHLtitle": "false",
            },
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.cninfo.com.cn/"},
            timeout=min(self._timeout, deadline - self._monotonic()),
            deadline=deadline,
            cancel_requested=self._cancel_requested,
        )

    def split_queries(self, partition: DisclosurePartition, *, deadline: float) -> tuple[DisclosureQuery, ...]:
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
                    timeout=min(self._timeout, deadline - self._monotonic()),
                    deadline=deadline,
                    cancel_requested=self._cancel_requested,
                ),
                deadline,
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

    def _request(self, send: Callable[[], HttpResponse], deadline: float) -> HttpResponse:
        # Only the read-only CNInfo protocol owns retries; the shared transport remains single-attempt.
        for attempt in range(3):
            self._pace(deadline)
            try:
                response = send()
                CheckCompanyResponse(response)
                self._check(deadline)
                return response
            except (HttpTimeout, HttpConnectionError, CompanySourceFailure) as exc:
                if isinstance(exc, CompanySourceFailure) and exc.status_code not in {408, 500, 502, 503, 504}:
                    raise
                self._check(deadline)
                if attempt == 2:
                    raise
                self._next_request = max(self._next_request, self._monotonic() + 2 ** (attempt + 1))
        raise AssertionError("bounded CNInfo attempts must return or raise")

    def _check(self, deadline: float) -> None:
        if self._cancel_requested is not None and self._cancel_requested():
            raise HttpCancelled("http_cancelled")
        if self._monotonic() >= deadline:
            raise TimeoutError("disclosure_deadline")

    def _pace(self, deadline: float) -> None:
        self._check(deadline)
        delay = max(0.0, self._next_request - self._monotonic())
        if self._monotonic() + delay >= deadline:
            raise TimeoutError("disclosure_deadline")
        while delay > 0:
            interval = min(0.25, delay)
            self._sleep(interval)
            self._check(deadline)
            delay -= interval
        self._next_request = self._monotonic() + self._request_interval

    def document(self, item: Disclosure, *, deadline: float) -> str:
        if not item.url.startswith("https://static.cninfo.com.cn/finalpage/"):
            raise ValueError("disclosure_document_unavailable")
        try:
            response = self._request(
                lambda: self._session.get(
                    item.url,
                    timeout=min(self._timeout, deadline - self._monotonic()),
                    deadline=deadline,
                    max_response_bytes=self._document_limits.max_bytes,
                    cancel_requested=self._cancel_requested,
                ),
                deadline,
            )
        except HttpResponseTooLarge as exc:
            raise ValueError("disclosure_document_oversized") from exc
        remaining = min(self._document_parse_timeout, deadline - self._monotonic())
        if remaining <= 0:
            raise TimeoutError("disclosure_deadline")
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "trader.download.infra.company_document_parser",
                    "--max-bytes",
                    str(self._document_limits.max_bytes),
                    "--max-pages",
                    str(self._document_limits.max_pages),
                    "--max-text-characters",
                    str(self._document_limits.max_text_characters),
                ],
                input=response.content,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=remaining,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError("disclosure_document_parse_timeout") from exc
        if result.returncode:
            reason = {
                3: "encrypted",
                4: "page_limit",
                5: "expanded_page_limit",
                6: "text_limit",
                7: "no_text",
                8: "short_text",
            }
            raise ValueError(f"disclosure_document_{reason.get(result.returncode, 'unreadable')}")
        return result.stdout.decode("utf-8")


def _plate(code: str) -> str:
    if code.startswith("6"):
        return "shkcp" if code.startswith(("688", "689")) else "shmb"
    if code.startswith(("4", "8", "9")):
        return "bj"
    return "szcy" if code.startswith(("300", "301")) else "szmb"


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
