"""Money-independent score arithmetic, event verification and temporal boundaries."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from trader.download.domain.company_event_extraction import ExtractCompanyEvents
from trader.download.domain.company_events import CompanyDataState
from trader.download.domain.company_evidence import CompanyDocument, CompanyFinancial, CompanyRatio, Disclosure
from trader.download.domain.company_facts import CompanyFacts
from trader.recommendation.domain.publication.score_ledger import ScoreAdjustmentItem
from trader.recommendation.domain.scoring.company import AssessCompanyAdjustments, BuildCompanyScoringContext, CompanyScoringCalendar, CompanyAdjustments, CompanyFinancialScores
from tests.helpers.download.configuration import CompanyEventPolicyFixture
from tests.helpers.recommendation.configuration import CompanyScoringPolicyFixture

AT = datetime(2026, 10, 27, 10, tzinfo=ZoneInfo("Asia/Shanghai"))


def CompanyEventPolicy(**changes):
    return CompanyEventPolicyFixture(**changes)


def CompanyScoringPolicy(**changes):
    return CompanyScoringPolicyFixture(**changes)


def _adjustment(points):
    return CompanyAdjustments(
        (ScoreAdjustmentItem("buyback", "positive_bonus", "实际回购", 3, points, 0, "eligible"),), (), 0, AT.date()
    )


def _document(identity, title, body, at=AT):
    return CompanyDocument(
        Disclosure("cninfo", identity, ("600001",), title, at, "https://example.test/notice.pdf", identity), body, True
    )


def _facts(documents=(), at=AT):
    events = ExtractCompanyEvents(documents, at, CompanyEventPolicy())
    return CompanyFacts(
        "600001",
        CompanyFinancial("600001", at.date(), at, 1, 5, 25, 25, 25, 10, 100, 90),
        CompanyDataState.COMPLETE,
        CompanyRatio("600001", at.date(), 0),
        CompanyDataState.COMPLETE,
        (),
        at,
        at.date() + timedelta(days=180),
        CompanyDataState.COMPLETE,
        events.risks,
        events.positives,
        events.reviews,
        0,
        at.date() - timedelta(days=365),
        at.date(),
        CompanyDataState.COMPLETE,
        at,
        at,
    )


def _assess(facts, strategy="tomorrow", *, at=AT, enabled=True, calendar=True):
    policy = CompanyScoringPolicy(enabled=enabled)
    days = tuple((AT + timedelta(days=i)).date() for i in range(-5, 40) if (AT + timedelta(days=i)).weekday() < 5)
    context = BuildCompanyScoringContext(facts, CompanyScoringCalendar(days if calendar else ()), policy)
    return AssessCompanyAdjustments(
        context, policy, strategy, at, financial_scores=CompanyFinancialScores(100, 100, 100)
    )
