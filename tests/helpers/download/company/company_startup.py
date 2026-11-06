"""Company facts keep disclosure time separate from risk coverage and load time."""

import time
from dataclasses import replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from tests.helpers.download.configuration import CompanyEventPolicyFixture as CompanyEventPolicy
from trader.download.domain.company_evidence import (
    CompanyDocument,
    CompanyEvidenceInput,
    CompanyFinancial,
    CompanyRatio,
    Disclosure,
)
from trader.download.domain.company_facts import CompileCompanyFacts
from tests.helpers.recommendation.company_research import PublishedCompanyResearchFixture as PublishedCompanyResearch

_AT = datetime(2026, 9, 30, 20, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

_DATES = (date(2026, 9, 29), date(2026, 9, 30), date(2026, 10, 8), date(2026, 10, 9))


def _snapshot():
    report = CompanyFinancial("600001", date(2026, 6, 30), _AT, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0)
    future = replace(report, report_date=date(2026, 9, 30), published_at=_AT + timedelta(days=10))
    disclosure = Disclosure("cninfo", "investigation", ("600001",), "收到中国证监会立案告知书", _AT, "", "digest")
    return CompanyEvidenceInput(
        "600001",
        _AT,
        _AT.date() - timedelta(days=365),
        _AT.date(),
        "first",
        (CompanyDocument(disclosure, "本公司收到证监会立案告知书〔2026〕调查1号。", True),),
        True,
        (report, future),
        CompanyRatio("600001", _AT.date(), 10.0),
        (CompanyRatio("600001", date(2026, 10, 20), 12.0),),
        True,
        (),
        False,
        (),
        source_checked_at=_AT,
        unlock_checked_at=_AT,
        unlock_coverage_end=_AT.date() + timedelta(days=180),
    )


class _Reader:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.visits = 0

    def publication_version(self):
        return self.snapshot.version

    def visit(self, codes, consume, *, cancel_requested=None):
        self.visits += 1
        consume(CompileCompanyFacts(self.snapshot, CompanyEventPolicy()))


def _research(reader, now, monotonic=time.monotonic):
    return PublishedCompanyResearch(reader, wall_clock=lambda: now, open_dates=lambda: _DATES, monotonic=monotonic)
