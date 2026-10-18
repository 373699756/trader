"""HTTP fixtures verify actual adapters, units, pagination and bounded failure categories."""

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from trader.download.domain.company_evidence import (
    CompanySourceFailure,
    Disclosure,
    DisclosurePartition,
    StructuredCompanyQuery,
)
from trader.download.infra.company_disclosure_source import CninfoMarketDisclosureSource
from trader.download.infra.company_structured_source import EastmoneyCompanySource
from trader.infra.http_contracts import HttpResponse, HttpResponseTooLarge

NOW = datetime(2026, 10, 18, 9, 15, tzinfo=ZoneInfo("Asia/Shanghai"))


class Session:
    def __init__(self, payload, status=200):
        self.payload, self.status = payload, status
        self.requests = []

    def post(self, url, **kwargs):
        self.requests.append(kwargs)
        return HttpResponse(self.status, json.dumps(self.payload).encode())

    get = post


def test_cninfo_uses_market_dates_default_sort_and_actual_more_flag():
    session = Session(
        {
            "totalRecordNum": 31,
            "totalpages": 1,
            "hasMore": True,
            "announcements": [
                {
                    "announcementId": "one",
                    "secCode": "600001",
                    "announcementTitle": "普通公告",
                    "announcementTime": NOW.timestamp() * 1000,
                    "adjunctUrl": "finalpage/2026-10-18/one.PDF",
                }
            ],
        }
    )
    source = CninfoMarketDisclosureSource(session, monotonic=lambda: 0, sleeper=lambda value: None)
    page = source.fetch(DisclosurePartition("sse", NOW.date(), NOW.date()), deadline=8)
    assert page.has_more and page.total == 31
    assert page.rows[0].codes == ("600001",)
    query = session.requests[0]["data"]
    assert query["stock"] == query["sortName"] == query["sortType"] == ""
    assert query["pageSize"] == "30"
    assert session.requests[0]["timeout"] == 8


@pytest.mark.parametrize(("status", "category"), ((403, "access_denied"), (429, "rate_limited"), (500, "http_error")))
def test_rejections_have_distinct_safe_categories(status, category):
    source = CninfoMarketDisclosureSource(Session({}, status), monotonic=lambda: 0, sleeper=lambda value: None)
    with pytest.raises(CompanySourceFailure) as caught:
        source.fetch(DisclosurePartition("sse", NOW.date(), NOW.date()), deadline=8)
    assert caught.value.category == category
    assert caught.value.status_code == status


def test_oversized_pdf_remains_a_document_failure_instead_of_stopping_the_source():
    class Oversized:
        def get(self, *args, **kwargs):
            raise HttpResponseTooLarge("http_response_too_large")

    source = CninfoMarketDisclosureSource(Oversized(), monotonic=lambda: 0, sleeper=lambda value: None)
    item = Disclosure(
        "cninfo", "fixture", ("600001",), "风险公告", NOW, "https://static.cninfo.com.cn/finalpage/a.PDF", "a"
    )
    with pytest.raises(ValueError, match="disclosure_document_oversized"):
        source.document(item, deadline=8)


def test_financial_non_a_shares_are_excluded_without_corrupting_raw_page_count():
    rows = [
        {
            "SECUCODE": code,
            "REPORT_DATE": "2026-06-30",
            "NOTICE_DATE": "2026-10-17",
            "PARENTNETPROFIT": -2,
            "KCFJCXSYJLR": 3,
        }
        for code in ("600001.SH", "XYZ.SH", "00001.HK")
    ]
    session = Session({"result": {"count": 3, "pages": 1, "data": rows}})
    source = EastmoneyCompanySource(session, monotonic=lambda: 0)
    page = source.fetch(
        StructuredCompanyQuery("financial", date(2026, 10, 11), NOW.date()), observed_at=NOW, deadline=8
    )
    assert page.row_count == 3
    assert len(page.financials) == 1
    assert page.financials[0].parent_net_profit == -2
    assert page.financials[0].code == "600001"


def test_unlock_source_converts_fraction_to_percentage():
    session = Session(
        {
            "result": {
                "count": 1,
                "pages": 1,
                "data": [
                    {
                        "SECURITY_CODE": "600001",
                        "FREE_DATE": "2026-10-19",
                        "TOTAL_RATIO": 0.031,
                    }
                ],
            }
        }
    )
    source = EastmoneyCompanySource(session, monotonic=lambda: 0)
    page = source.fetch(StructuredCompanyQuery("unlock", NOW.date(), date(2027, 1, 16)), observed_at=NOW, deadline=8)
    assert page.ratios[0].ratio_pct == pytest.approx(3.1)


def test_broken_empty_payload_is_not_a_successful_zero_snapshot():
    source = EastmoneyCompanySource(Session({"result": None}), monotonic=lambda: 0)
    with pytest.raises(ValueError, match="result_missing"):
        source.fetch(StructuredCompanyQuery("pledge", NOW.date(), NOW.date()), observed_at=NOW, deadline=8)
