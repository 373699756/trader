"""Market-wide financial updates, dated pledge snapshots, unlocks and candidate news."""

from __future__ import annotations

import hashlib
import html
import json
import math
import re
from collections.abc import Callable, Mapping
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from trader.download.domain.company_evidence import (
    CompanyFinancial,
    CompanyRatio,
    Disclosure,
    StructuredCompanyPage,
    StructuredCompanyQuery,
)
from trader.download.infra.company_http import CheckCompanyResponse
from trader.infra.http_contracts import HttpCancelled, HttpTransport

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_DATA_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"


class EastmoneyCompanySource:
    def __init__(
        self,
        session: HttpTransport,
        *,
        monotonic: Callable[[], float],
        timeout_seconds: float,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> None:
        self._session = session
        self._monotonic = monotonic
        self._timeout = timeout_seconds
        self._cancel_requested = cancel_requested

    def _json(self, url: str, params: Mapping[str, str]) -> Mapping[str, object]:
        self._check_cancelled()
        response = self._session.get(
            url,
            params=params,
            timeout=self._timeout,
            deadline=self._monotonic() + self._timeout,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"},
            cancel_requested=self._cancel_requested,
        )
        self._check_cancelled()
        CheckCompanyResponse(response)
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise ValueError("company_source_schema_invalid")
        return payload

    def _check_cancelled(self) -> None:
        if self._cancel_requested is not None and self._cancel_requested():
            raise HttpCancelled("http_cancelled")

    def pledge_day(self, observed_at: datetime) -> date:
        payload = self._json(
            _DATA_URL,
            {
                "reportName": "RPT_CSDC_LIST",
                "columns": "TRADE_DATE",
                "sortColumns": "TRADE_DATE",
                "sortTypes": "-1",
                "pageNumber": "1",
                "pageSize": "1",
                "source": "WEB",
                "client": "WEB",
                "filter": f"(TRADE_DATE<='{observed_at.date().isoformat()}')",
            },
        )
        rows, _, _ = _result(payload)
        if not rows:
            raise ValueError("pledge_snapshot_unavailable")
        day = _day(rows[0].get("TRADE_DATE"))
        if day > observed_at.date():
            raise ValueError("pledge_snapshot_future")
        return day

    def fetch(self, query: StructuredCompanyQuery, *, observed_at: datetime) -> StructuredCompanyPage:
        component, start, end, page = query.component, query.start, query.end, query.page
        if component == "financial":
            payload = self._json(
                "https://datacenter.eastmoney.com/securities/api/data/get",
                {
                    "type": "RPT_F10_FINANCE_MAINFINADATA",
                    "sty": "SECUCODE,REPORT_DATE,NOTICE_DATE,EPSJB,BPS,TOTALOPERATEREVETZ,PARENTNETPROFITTZ,KCFJCXSYJLRTZ,ROEJQ,PARENTNETPROFIT,KCFJCXSYJLR",
                    "quoteColumns": "",
                    "filter": f"(REPORT_DATE>='{(observed_at.date() - timedelta(days=550)).isoformat()}')"
                    f"(NOTICE_DATE>='{start.isoformat()}')(NOTICE_DATE<='{end.isoformat()}')",
                    "p": str(page),
                    "ps": "500",
                    "sr": "1,1,1",
                    "st": "NOTICE_DATE,SECUCODE,REPORT_DATE",
                    "source": "HSF10",
                    "client": "PC",
                },
            )
        else:
            field = "TRADE_DATE" if component == "pledge" else "FREE_DATE"
            payload = self._json(
                _DATA_URL,
                {
                    "reportName": "RPT_CSDC_LIST" if component == "pledge" else "RPT_LIFT_STAGE",
                    "columns": "SECURITY_CODE,TRADE_DATE,PLEDGE_RATIO"
                    if component == "pledge"
                    else "SECURITY_CODE,FREE_DATE,TOTAL_RATIO",
                    "sortColumns": f"{field},SECURITY_CODE",
                    "sortTypes": "1,1",
                    "pageNumber": str(page),
                    "pageSize": "500",
                    "source": "WEB",
                    "client": "WEB",
                    "filter": f"({field}>='{start.isoformat()}')({field}<='{end.isoformat()}')",
                },
            )
        rows, pages, total = _result(payload)
        if pages > 256 or page > pages:
            raise ValueError("company_source_page_limit")
        if component == "financial":
            reports = tuple(_financial(row) for row in rows if _is_a_share(row))
            if any(not start <= item.published_at.date() <= end for item in reports):
                raise ValueError("financial_date_filter_ignored")
            return StructuredCompanyPage(component, page, pages, total, financials=reports, raw_count=len(rows))
        ratios: list[CompanyRatio] = []
        for row in rows:
            code = _code(row)
            day = _day(row.get("TRADE_DATE" if component == "pledge" else "FREE_DATE"))
            ratio = _number(row.get("PLEDGE_RATIO" if component == "pledge" else "TOTAL_RATIO"))
            if ratio is None or not start <= day <= end:
                raise ValueError("company_ratio_invalid")
            if component == "unlock":
                ratio *= 100.0
            if not 0 <= ratio <= 100:
                raise ValueError("company_ratio_out_of_range")
            ratios.append(CompanyRatio(code, day, ratio))
        return StructuredCompanyPage(component, page, pages, total, ratios=tuple(ratios))

    def news(self, code: str, *, observed_at: datetime) -> tuple[Disclosure, ...]:
        # Ordinary news is bounded candidate context, not an exhaustive regulatory feed.
        parameter = {
            "uid": "",
            "keyword": code,
            "type": ["cmsArticleWebOld"],
            "client": "web",
            "clientType": "web",
            "clientVersion": "curr",
            "param": {
                "cmsArticleWebOld": {
                    "searchScope": "default",
                    "sort": "time",
                    "pageIndex": 1,
                    "pageSize": 50,
                    "preTag": "",
                    "postTag": "",
                }
            },
        }
        self._check_cancelled()
        response = self._session.get(
            "https://search-api-web.eastmoney.com/search/jsonp",
            params={"cb": "company", "param": json.dumps(parameter, ensure_ascii=False)},
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://so.eastmoney.com/"},
            timeout=self._timeout,
            deadline=self._monotonic() + self._timeout,
            cancel_requested=self._cancel_requested,
        )
        self._check_cancelled()
        CheckCompanyResponse(response)
        text = response.text.strip()
        if not text.startswith("company("):
            raise ValueError("news_schema_invalid")
        raw = json.loads(text[len("company(") :].rstrip(";)").strip())
        if not isinstance(raw, Mapping):
            raise ValueError("news_schema_invalid")
        result = raw.get("result")
        rows = result.get("cmsArticleWebOld") if isinstance(result, Mapping) else None
        if not isinstance(rows, list):
            raise ValueError("news_rows_invalid")
        evidence: list[Disclosure] = []
        for row in rows:
            if (
                not isinstance(row, Mapping)
                or not isinstance(row.get("date"), str)
                or not isinstance(row.get("title"), str)
            ):
                raise ValueError("news_row_invalid")
            published = datetime.fromisoformat(row["date"])
            published = (
                published.replace(tzinfo=_SHANGHAI)
                if published.utcoffset() is None
                else published.astimezone(_SHANGHAI)
            )
            if not observed_at - timedelta(days=7) <= published <= observed_at:
                continue
            title = html.unescape(re.sub(r"<[^>]+>", "", str(row["title"]))).strip()
            url = str(row.get("url") or row.get("uniqueUrl") or "")
            identity = hashlib.sha256((url or f"{title}\n{published.isoformat()}").encode()).hexdigest()
            digest = hashlib.sha256(f"{title}\n{published.isoformat()}\n{url}".encode()).hexdigest()
            evidence.append(Disclosure("eastmoney_news", identity, (code,), title, published, url, digest, "news"))
        return tuple(evidence)


def _result(payload: Mapping[str, object]) -> tuple[tuple[Mapping[str, object], ...], int, int]:
    result = payload.get("result")
    if not isinstance(result, Mapping):
        raise ValueError("structured_source_result_missing")
    count, pages, rows = result.get("count"), result.get("pages"), result.get("data")
    if count == 0 and rows is None:
        rows = []
    if type(count) is not int or count < 0 or type(pages) is not int or pages < 0 or not isinstance(rows, list):
        raise ValueError("structured_source_pagination_invalid")
    if any(not isinstance(row, Mapping) for row in rows):
        raise ValueError("structured_source_rows_invalid")
    return tuple(rows), max(1, pages), count


def _day(value: object) -> date:
    return date.fromisoformat(str(value)[:10])


def _code(row: Mapping[str, object]) -> str:
    code = str(row.get("SECURITY_CODE") or row.get("SECUCODE") or "").split(".")[0]
    if len(code) != 6 or not code.isdigit():
        raise ValueError("company_source_code_invalid")
    return code


def _number(value: object) -> float | None:
    if value is None or value in ("", "-", "--"):
        return None
    if not isinstance(value, (str, float, int)) or isinstance(value, bool):
        raise ValueError("company_number_invalid")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("company_number_nonfinite")
    return number


def _financial(row: Mapping[str, object]) -> CompanyFinancial:
    return CompanyFinancial(
        _code({"SECUCODE": row["SECUCODE"]}),
        _day(row.get("REPORT_DATE")),
        datetime.combine(_day(row.get("NOTICE_DATE")), time.min, _SHANGHAI),
        _number(row.get("EPSJB")),
        _number(row.get("BPS")),
        _number(row.get("TOTALOPERATEREVETZ")),
        _number(row.get("PARENTNETPROFITTZ")),
        _number(row.get("KCFJCXSYJLRTZ")),
        _number(row.get("ROEJQ")),
        _number(row.get("PARENTNETPROFIT")),
        _number(row.get("KCFJCXSYJLR")),
    )


def _is_a_share(row: Mapping[str, object]) -> bool:
    identity = str(row.get("SECUCODE", "")).split(".")
    return (
        len(identity) == 2
        and identity[1] in {"SH", "SZ", "BJ"}
        and len(identity[0]) == 6
        and identity[0].isdigit()
        and identity[0].startswith(("0", "3", "6", "4", "8", "92"))
    )


__all__ = ["EastmoneyCompanySource"]
