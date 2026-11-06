"""Annual revenue travels through the real source decoder, ledger and formal reader."""

import json
from dataclasses import replace
from datetime import date, datetime
from zoneinfo import ZoneInfo
from tests.helpers.download.company import company_evidence as fixtures
from trader.download.domain.company_annual_income import CompanyAnnualIncome
from trader.download.domain.company_evidence import CompanyFinancial, DisclosurePage, DisclosurePartition, StructuredCompanyPage, StructuredCompanyQuery
from trader.download.infra.company_fact_rows import ReadCompanyFactRows
from trader.infra.http_contracts import HttpResponse, HttpTimeout
from trader.infra.sqlite_connection import SqliteSession

_ZONE = ZoneInfo("Asia/Shanghai")

_REPORTED = datetime(2026, 4, 17, tzinfo=_ZONE)

_SIGNED = datetime(2026, 5, 20, tzinfo=_ZONE)

_PERIOD = date(2025, 12, 31)

_OPERATING_REVENUE = 168_838_102_514.79

_TOTAL_OPERATING_INCOME = 172_054_171_890.91


def _financial(**changes):
    return replace(CompanyFinancial("600001", _PERIOD, _REPORTED, 1, 1, 1, 1, 1, 1, 1, 1), **changes)


def _report():
    return {
        "SECUCODE": "600001.SH", "SECURITY_CODE": "600001", "REPORT_DATE": "2025-12-31 00:00:00",
        "REPORT_TYPE": "年报", "NOTICE_DATE": "2026-04-17 00:00:00", "UPDATE_DATE": "2026-04-17 00:00:00",
        "CURRENCY": "CNY", "OPERATE_INCOME": _OPERATING_REVENUE,
        "TOTAL_OPERATE_INCOME": _TOTAL_OPERATING_INCOME,
    }


class _Session:
    def __init__(self, row=None, *, html=None):
        self.row = _report() if row is None else row
        self.html = '<input id="hidctype" value="4" />' if html is None else html
        self.requests = []

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        if url.endswith("/Index"):
            return HttpResponse(200, self.html.encode())
        return HttpResponse(200, json.dumps({"data": [self.row]}).encode())


def _income(**changes):
    return replace(CompanyAnnualIncome(
        "600001", _PERIOD, _REPORTED, _REPORTED, fixtures.NOW, "CNY", _OPERATING_REVENUE, "a" * 64
    ), **changes)


def _read(index):
    with SqliteSession(index.facts_path) as connection:
        return ReadCompanyFactRows(connection, ("600001",))[0]


def _contract(index, *, reference="公司2025年度经审计的营业收入", known_annual=True):
    item = replace(
        fixtures._disclosure("contract-income", "关于签订重大合同的公告"), published_at=_SIGNED
    )
    partition = DisclosurePartition("sse", _SIGNED.date(), _SIGNED.date())
    index.commit_page(partition, DisclosurePage((item,), 1, False), fixtures.NOW)
    text = (
        "本公司已签订正式合同，合同已生效。合同金额人民币2亿元（不含税）。"
        f"本合同的金额与{reference}进行比较。"
        "本公告仅说明上述合同的既有事实，公司将按照相关规定持续披露后续进展，相关资料以本次正式披露为准。"
    )
    index.commit_document(item, fixtures._extraction(text), fixtures.NOW)
    if known_annual:
        financial = _financial()
        query = StructuredCompanyQuery("financial", _REPORTED.date(), fixtures.NOW.date())
        index.commit_structured(query, StructuredCompanyPage("financial", 1, 1, 1, financials=(financial,)), fixtures.NOW)
    index.publish(item.codes, fixtures.NOW)
    return item


class _Sources(fixtures.Sources):
    def __init__(self, item, *, fail=False):
        super().__init__()
        self.item, self.fail = item, fail
        self.annual_calls = 0

    def fetch(self, query, **kwargs):
        if isinstance(query, DisclosurePartition):
            present = query.market == "sse" and query.start <= self.item.published_at.date() <= query.end
            return DisclosurePage((self.item,) if present else (), int(present), False)
        return super().fetch(query, **kwargs)

    def annual_income(self, request, *, observed_at):
        self.annual_calls += 1
        if self.fail:
            raise HttpTimeout("fixture_timeout")
        return (_income(observed_at=observed_at),)
