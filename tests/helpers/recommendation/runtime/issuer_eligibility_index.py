"""Reference-owned ST stays in memory; non-ST exclusions enter company facts."""

from datetime import datetime
from zoneinfo import ZoneInfo
from trader.download.application.company_exclusions import PublishCompanyExclusions
from tests.helpers.download.company_resources import SQLiteCompanyFactsIndexFixture as SQLiteCompanyFactsIndex
from trader.recommendation.domain.market.eligibility import IssuerEligibilityFact, IssuerEligibilityReason
from trader.recommendation.infra.market_data.company_eligibility import CompanyEligibility

AT = datetime(2026, 9, 1, 10, tzinfo=ZoneInfo("Asia/Shanghai"))


def _fact():
    return IssuerEligibilityFact(
        "600001", IssuerEligibilityReason.HISTORICAL_DELISTING_WARNING, AT, "quote:delisting", "market_quote", "a" * 64
    )


def _reader(path):
    return CompanyEligibility(PublishCompanyExclusions(SQLiteCompanyFactsIndex(path)))
