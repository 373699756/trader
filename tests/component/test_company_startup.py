"""Company facts keep disclosure time separate from risk coverage and load time."""

import time
from dataclasses import replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.helpers.configuration import CompanyEventPolicyFixture as CompanyEventPolicy
from trader.download.domain.company_evidence import (
    CompanyDocument,
    CompanyEvidenceInput,
    CompanyFinancial,
    CompanyRatio,
    Disclosure,
)
from trader.download.domain.company_facts import CompileCompanyFacts
from trader.recommendation.domain.market.research import DeriveCorporateRiskFeatures, ResearchComponentsFromObservation
from tests.helpers.configuration import PublishedCompanyResearchFixture as PublishedCompanyResearch

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


@pytest.mark.parametrize(
    ("title", "known_risk"),
    (("控股股东收到立案告知书", True), ("关于收到立案告知书的公告", False)),
)
def test_holder_investigation_survives_issuer_denial_into_recommendation_features(title, known_risk):
    snapshot = _snapshot()
    document = snapshot.announcements[0]
    document = replace(
        document,
        disclosure=replace(document.disclosure, title=title),
        text="控股股东收到证监会立案告知书〔2026〕调查1号。本公司未被立案。",
    )
    research = _research(_Reader(replace(snapshot, announcements=(document,))), _AT)
    research.initialize(("600001",), cancelled=lambda: False)
    observation = research.load(("600001",), _AT, include_structured=True)["600001"]
    features = DeriveCorporateRiskFeatures(
        observation.corporate_risk_facts, _AT, window_complete=observation.corporate_risk_window_complete
    )
    assert features["official_investigation_history"] == float(known_risk)
    if not known_risk:
        assert not observation.corporate_risk_window_complete
        assert "risk:subject_unverified" in observation.source_errors


def test_previous_actual_session_survives_holiday_then_expires_only_risk_coverage():
    reader = _Reader(_snapshot())
    opening = datetime(2026, 10, 8, 9, 30, tzinfo=_AT.tzinfo)
    research = _research(reader, opening)
    research.initialize(("600001",), cancelled=lambda: False)
    restrictions = {}
    first = research.load(("600001",), opening, include_structured=True, action_restrictions=restrictions)["600001"]
    assert first.corporate_risk_window_complete
    assert len(first.financial_history) == 1
    assert first.loaded_at == opening and first.snapshot_at == _AT
    assert first.queried_on == date(2026, 9, 30)
    assert first.unlock_ratio_pct is None
    assert first.unlock_window_end < opening.date() + timedelta(days=180)
    assert ResearchComponentsFromObservation(first).unlock == "unknown"
    assert restrictions == {"600001": {"research_data_degraded"}}
    assert first.corporate_risk_facts
    assert all(not item.body for item in first.announcements)
    restrictions = {}
    later = research.load(
        ("600001",), opening + timedelta(days=1), include_structured=True, action_restrictions=restrictions
    )["600001"]
    assert later.financial_history == first.financial_history
    assert ResearchComponentsFromObservation(later).financial == "known_clear"
    assert later.corporate_risk_facts == first.corporate_risk_facts
    assert not later.corporate_risk_window_complete
    assert restrictions == {"600001": {"research_data_degraded"}}
    assert reader.visits == 1


def test_unlock_horizon_is_requalified_in_memory_at_each_requested_date():
    reader = _Reader(_snapshot())
    research = _research(reader, _AT)
    research.initialize(("600001",), cancelled=lambda: False)
    initial = research.cached(("600001",), include_structured=True, as_of=_AT)["600001"]
    assert initial.unlock_ratio_pct == 12.0
    restrictions = {}
    later = research.cached(
        ("600001",), include_structured=True, as_of=_AT + timedelta(days=1), action_restrictions=restrictions
    )["600001"]
    assert later.corporate_risk_window_complete  # The prior trading session still covers announcements.
    assert later.unlock_ratio_pct is None and "unlock:coverage_incomplete" in later.source_errors
    assert not later.company_context.coverage_current
    assert restrictions == {"600001": {"research_data_degraded"}} and reader.visits == 1


def test_changed_publication_retries_without_accepting_mixed_rows():
    class Changing(_Reader):
        def visit(self, codes, consume, **kwargs):
            super().visit(codes, consume, **kwargs)
            self.snapshot = replace(self.snapshot, version="second")

    reader = Changing(_snapshot())
    research = _research(reader, _AT)
    research.initialize(("600001",), cancelled=lambda: False)
    assert reader.visits == 2
    assert research.status().loaded_identity == "second"
    assert research.load(("600001",), _AT, include_structured=True)["600001"].data_version == "second"
    reader.snapshot = replace(reader.snapshot, version="third", pledge=CompanyRatio("600001", _AT.date(), 80.0))
    research.initialize(("600001",), cancelled=lambda: False)
    assert not research.status().restart_required
    assert research.status().disk_identity == "second"
    assert research.load(("600001",), _AT, include_structured=True)["600001"].pledge_ratio_pct == 10.0
    assert reader.visits == 2


def test_preload_deadline_rejects_partial_company_projection():
    clock = [0.0]

    class Slow(_Reader):
        def visit(self, codes, consume, **kwargs):
            super().visit(codes, consume, **kwargs)
            clock[0] = 31.0

    research = _research(Slow(_snapshot()), _AT, lambda: clock[0])
    research.initialize(("600001",), cancelled=lambda: False)
    assert research.status().entries == 0
    assert research.status().last_error == "company_preload_deadline"
