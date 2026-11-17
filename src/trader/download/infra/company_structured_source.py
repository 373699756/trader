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
    EvidenceMarket,
)
from trader.download.domain.company_annual_income import CompanyAnnualIncome, CompanyAnnualIncomeRequest
from trader.download.domain.company_source_records import (
    CompanyFinancialMissingField, CompanyFinancialSourceObservation, CompanyFinancialSourcePage, CompanyFinancialSourceValues,
    CompanyFinancialSourceRecordHash,
)
from trader.download.domain.download_failure import DownloadValidationFailure
from trader.download.infra.company_http import CheckCompanyResponse
from trader.infra.http_contracts import HttpCancelled, HttpTransport
from trader.infra.serialization.canonical_codec import CanonicalDigest

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_DATA_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_FINANCIAL_COLUMNS = (
    "SECUCODE", "REPORT_DATE", "NOTICE_DATE", "EPSJB", "BPS", "TOTALOPERATEREVETZ", "PARENTNETPROFITTZ",
    "KCFJCXSYJLRTZ", "ROEJQ", "PARENTNETPROFIT", "KCFJCXSYJLR",
)
COMPANY_FINANCIAL_SOURCE_COLUMNS = (
    "SECUCODE", "SECURITY_CODE", "REPORT_DATE", "REPORT_TYPE", "NOTICE_DATE", "UPDATE_DATE", "CURRENCY",
    "EPSJB", "BPS", "TOTALOPERATEREVETZ", "PARENTNETPROFITTZ", "KCFJCXSYJLRTZ", "ROEJQ", "PARENTNETPROFIT", "KCFJCXSYJLR",
)


class EastmoneyCompanySource:
    source = "eastmoney"
    news_source = "eastmoney_news"

    def request_endpoint(self, component: str) -> str:
        """Describe the configured request route without parameters or initiating a request."""
        return {
            "financial": "https://datacenter.eastmoney.com/securities/api/data/get",
            "pledge": _DATA_URL,
            "unlock": _DATA_URL,
            "news": "https://search-api-web.eastmoney.com/search/jsonp",
            "annual_income": "Eastmoney.F10.NewFinanceAnalysis.interface_family",
        }.get(component, "")

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

    def _json(
        self, url: str, params: Mapping[str, str], *, max_response_bytes: int = 16 * 1024 * 1024
    ) -> Mapping[str, object]:
        return self._json_response(url, params, max_response_bytes=max_response_bytes)[0]

    def _json_response(
        self, url: str, params: Mapping[str, str], *, max_response_bytes: int,
    ) -> tuple[Mapping[str, object], bytes]:
        self._check_cancelled()
        response = self._session.get(
            url,
            params=params,
            timeout=self._timeout,
            deadline=self._monotonic() + self._timeout,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"},
            cancel_requested=self._cancel_requested,
            max_response_bytes=max_response_bytes,
        )
        self._check_cancelled()
        CheckCompanyResponse(response)
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise DownloadValidationFailure("company_source_json_invalid", "json", "valid_json", f"line_{exc.lineno}_column_{exc.colno}") from exc
        if not isinstance(payload, Mapping):
            raise DownloadValidationFailure("company_source_schema_invalid", "root", "mapping", type(payload).__name__)
        return payload, response.content

    def _check_cancelled(self) -> None:
        if self._cancel_requested is not None and self._cancel_requested():
            raise HttpCancelled("http_cancelled")

    def annual_income(
        self, request: CompanyAnnualIncomeRequest, *, observed_at: datetime
    ) -> tuple[CompanyAnnualIncome, ...]:
        """Read only explicitly requested annual reports through the F10 report route."""
        self._check_cancelled()
        market = {"sse": "SH", "szse": "SZ", "bse": "BJ"}[EvidenceMarket(request.code)]
        identity = market + request.code
        response = self._session.get(
            "https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/Index",
            params={"type": "web", "code": identity.lower()},
            timeout=self._timeout,
            deadline=self._monotonic() + self._timeout,
            max_response_bytes=2 * 1024 * 1024,
            cancel_requested=self._cancel_requested,
        )
        self._check_cancelled()
        CheckCompanyResponse(response)
        company_type = re.search(
            r"<input\b(?=[^>]*\bid=['\"]hidctype['\"])(?=[^>]*\bvalue=['\"]([1-4])['\"])[^>]*>",
            response.text,
            re.IGNORECASE,
        )
        if company_type is None:
            raise ValueError("company_annual_income_company_type_missing")
        payload = self._json(
            "https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/lrbAjaxNew",
            {
                "companyType": company_type[1], "reportDateType": "1", "reportType": "1",
                "dates": ",".join(day.isoformat() for day in request.report_dates), "code": identity,
            },
            max_response_bytes=2 * 1024 * 1024,
        )
        rows = payload.get("data")
        if not isinstance(rows, list) or len(rows) != len(request.report_dates):
            raise ValueError("company_annual_income_reports_missing")
        incomes: list[CompanyAnnualIncome] = []
        for row in rows:
            if not isinstance(row, Mapping) or row.get("SECUCODE") != f"{request.code}.{market}" or row.get("REPORT_TYPE") != "年报":
                raise ValueError("company_annual_income_report_identity_invalid")
            report_date = _day(row.get("REPORT_DATE"))
            currency = row.get("CURRENCY")
            if report_date not in request.report_dates or not isinstance(currency, str) or not currency:
                raise ValueError("company_annual_income_report_identity_invalid")
            disclosed = _income_time(row.get("NOTICE_DATE"))
            updated = _income_time(row.get("UPDATE_DATE"))
            if updated.date() < observed_at.date() - timedelta(days=365):
                raise ValueError("company_annual_income_outside_window")
            revenue = _number(row.get("OPERATE_INCOME"), field="OPERATE_INCOME")
            version = CanonicalDigest((request.code, report_date, disclosed, updated, currency, revenue))
            incomes.append(CompanyAnnualIncome(
                request.code,
                report_date,
                disclosed,
                updated,
                observed_at,
                currency,
                revenue,
                version,
            ))
        if {row.report_date for row in incomes} != set(request.report_dates):
            raise ValueError("company_annual_income_reports_missing")
        return tuple(incomes)

    def pledge_day(self, observed_at: datetime) -> date:
        window_start = observed_at.date() - timedelta(days=365)
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
                "filter": f"(TRADE_DATE>='{window_start.isoformat()}')(TRADE_DATE<='{observed_at.date().isoformat()}')",
            },
        )
        rows, _, _ = _result(payload)
        if not rows:
            raise ValueError("pledge_snapshot_unavailable")
        if len(rows) != 1:
            raise DownloadValidationFailure("pledge_snapshot_rows_invalid", "data.length", "one_row", len(rows))
        day = _day(rows[0].get("TRADE_DATE"))
        if day > observed_at.date():
            raise DownloadValidationFailure("pledge_snapshot_future", "TRADE_DATE", f"{window_start}..{observed_at.date()}", day.isoformat())
        if day < window_start:
            raise DownloadValidationFailure("pledge_snapshot_outside_window", "TRADE_DATE", f"{window_start}..{observed_at.date()}", day.isoformat())
        return day

    def fetch(self, query: StructuredCompanyQuery, *, observed_at: datetime) -> StructuredCompanyPage:
        component, start, end, page = query.component, query.start, query.end, query.page
        if component == "financial":
            if observed_at.utcoffset() is None:
                raise ValueError("company_financial_observation_time_invalid")
            start = max(start, observed_at.astimezone(_SHANGHAI).date() - timedelta(days=365))
            payload = self._json(
                "https://datacenter.eastmoney.com/securities/api/data/get",
                {
                    "type": "RPT_F10_FINANCE_MAINFINADATA",
                    "sty": ",".join(_FINANCIAL_COLUMNS),
                    "quoteColumns": "",
                    "filter": f"(NOTICE_DATE>='{start.isoformat()}')(NOTICE_DATE<='{end.isoformat()}')",
                    "p": str(page),
                    "ps": "500",
                    "sr": "-1,1,-1",
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
        if component == "financial":
            return DecodeCompanyFinancialPage(payload, query, observed_at=observed_at)
        rows, pages, total = _page_rows(payload, page)
        ratios: list[CompanyRatio] = []
        seen: set[tuple[str, date]] = set()
        for row in rows:
            code = _code(row)
            date_field = "TRADE_DATE" if component == "pledge" else "FREE_DATE"
            day = _day(row.get(date_field))
            if (code, day) in seen:
                raise DownloadValidationFailure("company_ratio_duplicate", "security_code.date", "unique_key", "duplicate")
            seen.add((code, day))
            ratio_field = "PLEDGE_RATIO" if component == "pledge" else "TOTAL_RATIO"
            ratio = _number(row.get(ratio_field), field=ratio_field)
            if ratio is None:
                raise DownloadValidationFailure("company_ratio_invalid", ratio_field, "number", "null")
            if not start <= day <= end:
                raise DownloadValidationFailure("company_ratio_invalid", date_field, f"{start}..{end}", day.isoformat())
            raw_ratio = ratio
            if component == "unlock":
                ratio *= 100.0
            if not 0 <= ratio <= 100:
                expected = "0..1_fraction" if component == "unlock" else "0..100_percent"
                raise DownloadValidationFailure("company_ratio_out_of_range", ratio_field, expected, str(raw_ratio))
            ratios.append(CompanyRatio(code, day, ratio))
        return StructuredCompanyPage(component, page, pages, total, ratios=tuple(ratios))

    def financial_observations(
        self, query: StructuredCompanyQuery, *, observed_at: datetime,
    ) -> CompanyFinancialSourcePage:
        """Explicit metadata observation; the production eleven-column fetch is unchanged."""
        _validate_financial_source_query(query, observed_at)
        payload, content = self._json_response(
            "https://datacenter.eastmoney.com/securities/api/data/get",
            {
                "type": "RPT_F10_FINANCE_MAINFINADATA", "sty": ",".join(COMPANY_FINANCIAL_SOURCE_COLUMNS),
                "quoteColumns": "", "filter": f"(NOTICE_DATE>='{query.start.isoformat()}')(NOTICE_DATE<='{query.end.isoformat()}')",
                "p": str(query.page), "ps": "500", "sr": "-1,1,-1", "st": "NOTICE_DATE,SECUCODE,REPORT_DATE",
                "source": "HSF10", "client": "PC",
            },
            max_response_bytes=1024 * 1024,
        )
        return DecodeCompanyFinancialSourcePage(
            payload, query, observed_at=observed_at, response_hash=hashlib.sha256(content).hexdigest(),
        )

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
        try:
            raw = json.loads(text[len("company(") :].rstrip(";)").strip())
        except json.JSONDecodeError as exc:
            raise DownloadValidationFailure("news_schema_invalid", "json", "valid_json", f"line_{exc.lineno}_column_{exc.colno}") from exc
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


def DecodeCompanyFinancialPage(
    payload: Mapping[str, object],
    query: StructuredCompanyQuery,
    *,
    observed_at: datetime,
) -> StructuredCompanyPage:
    """Decode the production financial page without performing I/O."""
    if query.component != "financial":
        raise ValueError("company_financial_page_component_invalid")
    if observed_at.utcoffset() is None:
        raise ValueError("company_financial_observation_time_invalid")
    start, end = max(query.start, observed_at.astimezone(_SHANGHAI).date() - timedelta(days=365)), query.end
    rows, pages, total = _page_rows(payload, query.page)
    reports: list[CompanyFinancial] = []
    for row in rows:
        if "SECUCODE" not in row:
            raise DownloadValidationFailure("financial_source_fields_missing", "columns", "eleven_required_columns", "missing")
        if not _is_a_share(row):
            continue
        if any(field not in row for field in _FINANCIAL_COLUMNS):
            raise DownloadValidationFailure("financial_source_fields_missing", "columns", "eleven_required_columns", "missing")
        notice_day = _day(row.get("NOTICE_DATE"))
        if not start <= notice_day <= end or _income_time(row["NOTICE_DATE"]) > observed_at:
            raise DownloadValidationFailure("financial_date_filter_ignored", "NOTICE_DATE", f"{start}..{end}_and_cutoff", notice_day.isoformat())
        reports.append(_financial(row))
    return StructuredCompanyPage("financial", query.page, pages, total, financials=tuple(reports), raw_count=len(rows))


def DecodeCompanyFinancialSourcePage(
    payload: Mapping[str, object], query: StructuredCompanyQuery, *, observed_at: datetime, response_hash: str,
) -> CompanyFinancialSourcePage:
    """Preserve same-row metadata; neither supplier dates nor hashes prove issuer revisions."""
    _validate_financial_source_query(query, observed_at)
    if not isinstance(response_hash, str) or re.fullmatch(r"[0-9a-f]{64}", response_hash) is None:
        raise ValueError("company_financial_source_response_hash_invalid")
    page = DecodeCompanyFinancialPage(payload, query, observed_at=observed_at)
    rows, _, _ = _page_rows(payload, query.page)
    observations: list[CompanyFinancialSourceObservation] = []
    reports = iter(page.financials)
    for row in rows:
        if any(field not in row for field in COMPANY_FINANCIAL_SOURCE_COLUMNS):
            raise DownloadValidationFailure("financial_source_fields_missing", "columns", "fifteen_required_columns", "missing")
        identity, code = row["SECUCODE"], row["SECURITY_CODE"]
        if (not isinstance(identity, str) or not isinstance(code, str) or not code
            or identity.count(".") != 1 or identity.split(".")[0] != code or not identity.split(".")[1]):
            raise DownloadValidationFailure("financial_source_identity_invalid", "SECUCODE", "matching_source_security_identity", "invalid")
        _day(row["NOTICE_DATE"])
        notice = _income_time(row["NOTICE_DATE"])
        if not query.start <= notice.date() <= query.end or notice > observed_at:
            raise DownloadValidationFailure("financial_date_filter_ignored", "NOTICE_DATE", "requested_window_and_cutoff", "outside")
        if not _is_a_share(row):
            continue
        financial = next(reports)
        report_type = {3: "一季报", 6: "中报", 9: "三季报", 12: "年报"}.get(financial.report_date.month)
        if row["REPORT_TYPE"] != report_type or (financial.report_date.month, financial.report_date.day) not in {
            (3, 31), (6, 30), (9, 30), (12, 31),
        }:
            raise DownloadValidationFailure("financial_source_report_identity_invalid", "REPORT_TYPE", "matching_quarter_end", "mismatch")
        if row["UPDATE_DATE"] is not None:
            _day(row["UPDATE_DATE"])
        updated = None if row["UPDATE_DATE"] is None else _income_time(row["UPDATE_DATE"])
        currency = row["CURRENCY"]
        if currency != "CNY":
            raise DownloadValidationFailure("financial_source_currency_unsupported", "CURRENCY", "CNY", "unsupported")
        try:
            observation = CompanyFinancialSourceObservation(
                financial, identity, notice, updated, observed_at, currency,
                CompanyFinancialSourceRecordHash(financial, identity, notice, updated, currency),
                tuple(CompanyFinancialMissingField(field) for field, value in CompanyFinancialSourceValues(financial) if value is None),
            )
        except ValueError as exc:
            if str(exc) not in {
                "company_financial_source_observation_invalid", "company_financial_source_time_invalid",
                "company_financial_source_hash_mismatch",
            }:
                raise
            raise DownloadValidationFailure(str(exc), "financial_source_row", "valid_source_observation", "invalid") from exc
        observations.append(observation)
    try:
        return CompanyFinancialSourcePage(query.page, page.pages, page.total, len(rows), tuple(observations), response_hash)
    except ValueError as exc:
        if str(exc) != "company_financial_source_page_invalid":
            raise
        raise DownloadValidationFailure(str(exc), "financial_source_page", "consistent_unique_source_rows", "invalid") from exc


def _validate_financial_source_query(query: StructuredCompanyQuery, observed_at: datetime) -> None:
    if (query.component != "financial" or observed_at.utcoffset() is None or query.page > 256
        or not observed_at.astimezone(_SHANGHAI).date() - timedelta(days=365)
        <= query.start <= query.end <= observed_at.astimezone(_SHANGHAI).date()):
        raise ValueError("company_financial_source_query_invalid")


def _page_rows(payload: Mapping[str, object], page: int) -> tuple[tuple[Mapping[str, object], ...], int, int]:
    rows, pages, total = _result(payload)
    if pages > 256:
        raise DownloadValidationFailure("company_source_page_limit", "pages", "1..256", pages)
    if page > pages:
        raise DownloadValidationFailure("company_source_page_limit", "page", f"1..{pages}", page)
    if len(rows) > 500 or total > pages * 500 or pages == 1 and len(rows) != total:
        raise DownloadValidationFailure("structured_source_pagination_invalid", "data.length", "consistent_bounded_page", len(rows))
    return rows, pages, total


def _result(payload: Mapping[str, object]) -> tuple[tuple[Mapping[str, object], ...], int, int]:
    code, success = payload.get("code"), payload.get("success")
    if type(code) is not int or code != 0:
        raise DownloadValidationFailure("structured_source_not_accepted", "code", 0, code if type(code) is int else type(code).__name__)
    if success is not True:
        raise DownloadValidationFailure("structured_source_not_accepted", "success", "true", str(success).lower() if type(success) is bool else type(success).__name__)
    result = payload.get("result")
    if not isinstance(result, Mapping):
        raise DownloadValidationFailure("structured_source_result_missing", "result", "mapping", type(result).__name__)
    count, pages, rows = result.get("count"), result.get("pages"), result.get("data")
    if count == 0 and rows is None:
        rows = []
    for field, value in (("count", count), ("pages", pages)):
        if type(value) is not int or value < 0:
            raise DownloadValidationFailure("structured_source_pagination_invalid", field, "nonnegative_integer", value if type(value) is int else type(value).__name__)
    if not isinstance(rows, list):
        raise DownloadValidationFailure("structured_source_pagination_invalid", "data", "list", type(rows).__name__)
    for position, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise DownloadValidationFailure("structured_source_rows_invalid", f"data.{position}", "mapping", type(row).__name__)
    assert isinstance(count, int) and isinstance(pages, int)
    if (
        count == 0 and (rows or pages not in (0, 1))
        or count > 0 and (not rows or not 1 <= pages <= count)
        or len(rows) > count
    ):
        raise DownloadValidationFailure("structured_source_pagination_invalid", "data.length", "consistent_count_and_pages", len(rows))
    return tuple(rows), max(1, pages), count


def _day(value: object) -> date:
    if not isinstance(value, str) or re.match(r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?:$|[ T])", value) is None:
        raise DownloadValidationFailure("company_source_date_invalid", "date", "ISO_date_or_timestamp", type(value).__name__)
    try:
        if len(value) == 10:
            return date.fromisoformat(value)
        parsed = datetime.fromisoformat(value)
        return parsed.date() if parsed.utcoffset() is None else parsed.astimezone(_SHANGHAI).date()
    except ValueError as exc:
        raise DownloadValidationFailure("company_source_date_invalid", "date", "ISO_date", type(value).__name__) from exc


def _income_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("company_annual_income_time_missing")
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=_SHANGHAI) if parsed.utcoffset() is None else parsed.astimezone(_SHANGHAI)


def _code(row: Mapping[str, object]) -> str:
    identity = row.get("SECUCODE")
    code = row.get("SECURITY_CODE") if "SECURITY_CODE" in row else identity.split(".")[0] if isinstance(identity, str) else None
    if not isinstance(code, str) or re.fullmatch(r"[0-9]{6}", code) is None:
        raise DownloadValidationFailure("company_source_code_invalid", "security_code", "six_ascii_digits", type(code).__name__)
    market = {"sse": "SH", "szse": "SZ", "bse": "BJ"}[EvidenceMarket(code)]
    if "SECUCODE" in row and identity != f"{code}.{market}":
        raise DownloadValidationFailure("company_source_identity_invalid", "SECUCODE", "matching_code_and_market", "mismatch")
    return code


def _number(value: object, *, field: str = "number") -> float | None:
    if value is None or value in ("", "-", "--"):
        return None
    if not isinstance(value, (str, float, int)) or isinstance(value, bool):
        raise DownloadValidationFailure("company_number_invalid", field, "number_or_null", type(value).__name__)
    try:
        number = float(value)
    except (ValueError, OverflowError) as exc:
        raise DownloadValidationFailure("company_number_invalid", field, "number_or_null", f"{type(value).__name__}_not_numeric") from exc
    if not math.isfinite(number):
        raise DownloadValidationFailure("company_number_nonfinite", field, "finite_number", "nonfinite")
    return number


def _financial(row: Mapping[str, object]) -> CompanyFinancial:
    return CompanyFinancial(
        _code(row),
        _day(row.get("REPORT_DATE")),
        datetime.combine(_day(row.get("NOTICE_DATE")), time.min, _SHANGHAI),
        _number(row.get("EPSJB"), field="EPSJB"),
        _number(row.get("BPS"), field="BPS"),
        _number(row.get("TOTALOPERATEREVETZ"), field="TOTALOPERATEREVETZ"),
        _number(row.get("PARENTNETPROFITTZ"), field="PARENTNETPROFITTZ"),
        _number(row.get("KCFJCXSYJLRTZ"), field="KCFJCXSYJLRTZ"),
        _number(row.get("ROEJQ"), field="ROEJQ"),
        _number(row.get("PARENTNETPROFIT"), field="PARENTNETPROFIT"),
        _number(row.get("KCFJCXSYJLR"), field="KCFJCXSYJLR"),
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


__all__ = [
    "COMPANY_FINANCIAL_SOURCE_COLUMNS",
    "DecodeCompanyFinancialPage",
    "DecodeCompanyFinancialSourcePage",
    "EastmoneyCompanySource",
]
