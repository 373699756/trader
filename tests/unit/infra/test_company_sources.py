"""HTTP fixtures verify actual adapters, units, pagination and bounded failure categories."""

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from trader.download.domain.company_evidence import (
    CompanySourceFailure,
    Disclosure,
    DisclosurePartition,
    DisclosureQuery,
    DisclosureSecurity,
    StructuredCompanyQuery,
)
from tests.helpers.configuration import (
    CninfoMarketDisclosureSourceFixture as CninfoMarketDisclosureSource,
    EastmoneyCompanySourceFixture as EastmoneyCompanySource,
)
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
    page = source.fetch(DisclosurePartition("sse", NOW.date(), NOW.date()))
    assert page.has_more and page.total == 31
    assert page.rows[0].codes == ("600001",)
    query = session.requests[0]["data"]
    assert query["stock"] == query["sortName"] == query["sortType"] == ""
    assert query["pageSize"] == "30"
    assert session.requests[0]["timeout"] == 8


def test_cninfo_refuses_page_beyond_official_hundred_page_window_without_request():
    session = Session({})
    source = CninfoMarketDisclosureSource(session, monotonic=lambda: 0, sleeper=lambda _: None)
    with pytest.raises(ValueError, match="cninfo_page_limit"):
        source.fetch(DisclosurePartition("sse", NOW.date(), NOW.date(), page=101))
    assert not session.requests


def test_cninfo_board_and_group_queries_use_official_filter_shape():
    session = Session({"totalRecordNum": 0, "hasMore": False, "announcements": []})
    source = CninfoMarketDisclosureSource(session, monotonic=lambda: 0, sleeper=lambda _: None)
    securities = (DisclosureSecurity("600001", "one"), DisclosureSecurity("600002", "two"))
    query = DisclosureQuery("shmb", securities)
    source.fetch(DisclosurePartition("sse", NOW.date(), NOW.date(), query=query))
    request = session.requests[0]["data"]
    assert request["plate"] == "shmb" and request["stock"] == "600001,one;600002,two"
    assert source.split_queries(DisclosurePartition("sse", NOW.date(), NOW.date())) == (
        DisclosureQuery("shmb"),
        DisclosureQuery("shkcp"),
    )
    children = source.split_queries(DisclosurePartition("sse", NOW.date(), NOW.date(), query=query))
    assert tuple(q.securities for q in children) == ((securities[0],), (securities[1],))


def test_board_split_uses_frozen_official_members_and_rejects_an_unsplittable_security():
    class Lists(Session):
        def get(self, url, **kwargs):
            self.requests.append(kwargs)
            return HttpResponse(
                200, json.dumps({"stockList": [{"code": f"600{i:03d}", "orgId": str(i)} for i in range(65)]}).encode()
            )

    session = Lists({})
    source = CninfoMarketDisclosureSource(session, monotonic=lambda: 0, sleeper=lambda _: None)
    day = DisclosurePartition("sse", NOW.date(), NOW.date(), query=DisclosureQuery("shmb"))
    groups = source.split_queries(day)
    assert [len(q.securities) for q in groups] == [32, 32, 1]
    assert source.split_queries(day) == groups and len(session.requests) == 1
    with pytest.raises(ValueError, match="cninfo_query_unsplittable"):
        source.split_queries(DisclosurePartition("sse", NOW.date(), NOW.date(), query=groups[-1]))


@pytest.mark.parametrize(
    "query", [DisclosureQuery("shkcp"), DisclosureQuery("shmb", (DisclosureSecurity("600002", "other"),))]
)
def test_cninfo_ignored_query_filter_never_returns_a_trusted_page(query):
    session = Session(
        {
            "totalRecordNum": 1,
            "hasMore": False,
            "announcements": [
                {
                    "announcementId": "one",
                    "secCode": "600001",
                    "announcementTitle": "公告",
                    "announcementTime": NOW.timestamp() * 1000,
                    "adjunctUrl": "",
                }
            ],
        }
    )
    source = CninfoMarketDisclosureSource(session, monotonic=lambda: 0, sleeper=lambda _: None)
    with pytest.raises(ValueError, match="cninfo_query_filter_ignored"):
        source.fetch(DisclosurePartition("sse", NOW.date(), NOW.date(), query=query))


def test_cninfo_historical_code_alias_requires_matching_official_issuer():
    row = {
        "announcementId": "one",
        "secCode": "600002",
        "orgId": "issuer",
        "announcementTitle": "公告",
        "announcementTime": NOW.timestamp() * 1000,
        "adjunctUrl": "",
    }
    session = Session({"totalRecordNum": 1, "hasMore": False, "announcements": [row]})
    source = CninfoMarketDisclosureSource(session, monotonic=lambda: 0, sleeper=lambda _: None)
    query = DisclosureQuery("shmb", (DisclosureSecurity("600001", "issuer"),))
    partition = DisclosurePartition("sse", NOW.date(), NOW.date(), query=query)
    assert source.fetch(partition).rows[0].codes == ("600002",)
    row["orgId"] = "unrelated"
    with pytest.raises(ValueError, match="cninfo_query_filter_ignored"):
        source.fetch(partition)


def test_chinext_302_announcements_and_issuer_groups_use_the_same_board():
    row = {
        "announcementId": "chinext",
        "secCode": "302001",
        "orgId": "issuer",
        "announcementTitle": "公告",
        "announcementTime": NOW.timestamp() * 1000,
        "adjunctUrl": "",
    }

    class ListedSession(Session):
        def get(self, url, **kwargs):
            return HttpResponse(200, json.dumps({"stockList": [
                {"code": "302001", "orgId": "issuer"},
                {"code": "000001", "orgId": "main"},
            ]}).encode())

    source = CninfoMarketDisclosureSource(
        ListedSession({"totalRecordNum": 1, "hasMore": False, "announcements": [row]}),
        monotonic=lambda: 0,
        sleeper=lambda _: None,
    )
    partition = DisclosurePartition("szse", NOW.date(), NOW.date(), query=DisclosureQuery("szcy"))
    assert source.fetch(partition).rows[0].codes == ("302001",)
    assert source.split_queries(partition) == (
        DisclosureQuery("szcy", (DisclosureSecurity("302001", "issuer"),)),
    )
    with pytest.raises(ValueError, match="cninfo_query_filter_ignored"):
        source.fetch(DisclosurePartition("szse", NOW.date(), NOW.date(), query=DisclosureQuery("szmb")))


def test_historical_issuer_discovery_keeps_known_members_and_stops_when_gap_is_found():
    row = {
        "announcementId": "old",
        "secCode": "600009",
        "orgId": "historical",
        "announcementTitle": "公告",
        "announcementTime": NOW.timestamp() * 1000,
        "adjunctUrl": "",
    }
    session = Session({"totalRecordNum": 4000, "hasMore": True, "announcements": [row]})
    source = CninfoMarketDisclosureSource(session, monotonic=lambda: 0, sleeper=lambda _: None)
    target = DisclosurePartition("sse", NOW.date(), NOW.date())
    queries = source.repair_queries(target, (DisclosureSecurity("600001", "current"),), missing_rows=1)
    assert queries == (DisclosureQuery("shmb", (DisclosureSecurity("600009", "historical"),)),)
    assert len(session.requests) == 1
    assert session.requests[0]["data"]["sortName"] == "code" and session.requests[0]["data"]["sortType"] == "asc"


def test_issuer_discovery_alternates_sort_edges_and_keeps_shanghai_b_share_coverage():
    class Sorted(Session):
        def post(self, url, **kwargs):
            self.requests.append(kwargs)
            descending = kwargs["data"]["sortType"] == "desc"
            row = {
                "announcementId": "old" if descending else "known",
                "secCode": "900901" if descending else "600001",
                "orgId": "historical" if descending else "current",
                "announcementTitle": "公告",
                "announcementTime": NOW.timestamp() * 1000,
                "adjunctUrl": "",
            }
            return HttpResponse(
                200, json.dumps({"totalRecordNum": 4000, "hasMore": True, "announcements": [row]}).encode()
            )

    session = Sorted({})
    source = CninfoMarketDisclosureSource(session, monotonic=lambda: 0, sleeper=lambda _: None)
    queries = source.repair_queries(
        DisclosurePartition("sse", NOW.date(), NOW.date()),
        (DisclosureSecurity("600001", "current"),),
        missing_rows=1,
    )
    assert queries == (DisclosureQuery("shmb", (DisclosureSecurity("900901", "historical"),)),)
    assert [(request["data"]["pageNum"], request["data"]["sortType"]) for request in session.requests] == [
        ("1", "asc"),
        ("1", "desc"),
    ]


@pytest.mark.parametrize(("status", "category"), ((403, "access_denied"), (429, "rate_limited"), (500, "http_error")))
def test_rejections_have_distinct_safe_categories(status, category):
    source = CninfoMarketDisclosureSource(Session({}, status), monotonic=lambda: 0, sleeper=lambda value: None)
    with pytest.raises(CompanySourceFailure) as caught:
        source.fetch(DisclosurePartition("sse", NOW.date(), NOW.date()))
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
        source.document(item)


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
    page = source.fetch(StructuredCompanyQuery("financial", date(2026, 10, 11), NOW.date()), observed_at=NOW)
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
    page = source.fetch(StructuredCompanyQuery("unlock", NOW.date(), date(2027, 1, 16)), observed_at=NOW)
    assert page.ratios[0].ratio_pct == pytest.approx(3.1)


def test_broken_empty_payload_is_not_a_successful_zero_snapshot():
    source = EastmoneyCompanySource(Session({"result": None}), monotonic=lambda: 0)
    with pytest.raises(ValueError, match="result_missing"):
        source.fetch(StructuredCompanyQuery("pledge", NOW.date(), NOW.date()), observed_at=NOW)
