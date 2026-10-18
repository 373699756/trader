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

from trader.download.domain.company_evidence import Disclosure, DisclosurePage, DisclosurePartition
from trader.download.infra.company_http import CheckCompanyResponse
from trader.infra.http_contracts import HttpTransport

_SHANGHAI = ZoneInfo("Asia/Shanghai")


class CninfoMarketDisclosureSource:
    def __init__(
        self,
        session: HttpTransport,
        *,
        monotonic: Callable[[], float],
        sleeper: Callable[[float], None],
        timeout_seconds: float = 8.0,
    ) -> None:
        self._session = session
        self._monotonic = monotonic
        self._sleep = sleeper
        self._timeout = timeout_seconds
        self._next_request = 0.0

    def fetch(self, partition: DisclosurePartition, *, deadline: float) -> DisclosurePage:
        if partition.page > 1000:
            raise ValueError("cninfo_single_day_page_limit")
        self._pace(deadline)
        response = self._session.post(
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
        )
        CheckCompanyResponse(response)
        if self._monotonic() >= deadline:
            raise TimeoutError("disclosure_deadline")
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

    def _pace(self, deadline: float) -> None:
        delay = max(0.0, self._next_request - self._monotonic())
        if self._monotonic() + delay >= deadline:
            raise TimeoutError("disclosure_deadline")
        if delay:
            self._sleep(delay)
        self._next_request = self._monotonic() + 1.0

    def document(self, item: Disclosure, *, deadline: float) -> str:
        if not item.url.startswith("https://static.cninfo.com.cn/finalpage/"):
            raise ValueError("disclosure_document_unavailable")
        self._pace(deadline)
        response = self._session.get(
            item.url,
            timeout=min(self._timeout, deadline - self._monotonic()),
            deadline=deadline,
            max_response_bytes=4 * 1024 * 1024,
        )
        CheckCompanyResponse(response)
        remaining = min(10.0, deadline - self._monotonic())
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
