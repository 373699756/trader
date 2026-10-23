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
    CompanySourceFailure,
    Disclosure,
    DisclosurePage,
    DisclosurePartition,
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
        cancel_requested: Callable[[], bool] | None = None,
    ) -> None:
        self._session = session
        self._monotonic = monotonic
        self._sleep = sleeper
        self._timeout = timeout_seconds
        self._document_parse_timeout = document_parse_timeout_seconds
        self._request_interval = request_interval_seconds
        self._cancel_requested = cancel_requested
        self._next_request = 0.0

    def fetch(self, partition: DisclosurePartition, *, deadline: float) -> DisclosurePage:
        if partition.page > 1000:
            raise ValueError("cninfo_single_day_page_limit")
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
                "plate": {"sse": "sh", "szse": "sz", "bse": "bj"}[partition.market],
                "tabName": "fulltext",
                "stock": "",
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
                    max_response_bytes=4 * 1024 * 1024,
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
                [sys.executable, "-m", "trader.download.infra.company_document_parser"],
                input=response.content,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=remaining,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError("disclosure_document_parse_timeout") from exc
        if result.returncode:
            raise ValueError("disclosure_document_unreadable")
        return result.stdout.decode("utf-8")


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
