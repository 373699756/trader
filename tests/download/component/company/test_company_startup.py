
from dataclasses import replace

from datetime import date, datetime, timedelta


import pytest


from trader.download.domain.company_evidence import CompanyRatio


from trader.recommendation.domain.market.research import DeriveCorporateRiskFeatures, ResearchComponentsFromObservation

from tests.helpers.recommendation.company_research import PublishedCompanyResearchFixture as PublishedCompanyResearch

from trader.infra.settings.models import CompanyPreloadSettings

from tests.helpers.download.company.company_startup import (
    _AT,
    _DATES,
    _Reader,
    _research,
    _snapshot,
)

import threading
from concurrent.futures import ThreadPoolExecutor
from trader.recommendation.infra.market_data import published_company_research


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


@pytest.mark.parametrize("attempts", (1, 4))
def test_configured_publication_attempts_bound_retries_without_adopting_mixed_rows(attempts):
    class Changing(_Reader):
        def visit(self, codes, consume, **kwargs):
            super().visit(codes, consume, **kwargs)
            self.snapshot = replace(self.snapshot, version=f"changed-{self.visits}")

    reader = Changing(_snapshot())
    research = PublishedCompanyResearch(
        reader,
        wall_clock=lambda: _AT,
        open_dates=lambda: _DATES,
        monotonic=lambda: 0.0,
        preload_settings=CompanyPreloadSettings(30, attempts),
    )
    research.initialize(("600001",), cancelled=lambda: False)
    assert reader.visits == attempts
    assert research.status().entries == 0
    assert research.status().last_error == "company_publication_changed"


@pytest.mark.parametrize("timeout,expected_entries", ((2, 0), (40, 1)))
def test_configured_company_timeout_controls_one_complete_startup_projection(timeout, expected_entries):
    clock = [0.0]

    class Slow(_Reader):
        def visit(self, codes, consume, **kwargs):
            super().visit(codes, consume, **kwargs)
            clock[0] = 3.0

    research = PublishedCompanyResearch(
        Slow(_snapshot()),
        wall_clock=lambda: _AT,
        open_dates=lambda: _DATES,
        monotonic=lambda: clock[0],
        preload_settings=CompanyPreloadSettings(timeout, 3),
    )
    research.initialize(("600001",), cancelled=lambda: False)
    assert research.status().entries == expected_entries


def test_cancelled_company_preload_does_not_visit_evidence():
    reader = _Reader(_snapshot())
    research = _research(reader, _AT)
    research.initialize(("600001",), cancelled=lambda: True)
    assert reader.visits == 0
    assert research.status().last_error == "company_preload_cancelled"


@pytest.mark.parametrize("blocked_operation", ("status", "cached"))
def test_company_qualification_does_not_hold_the_publication_lock(monkeypatch, blocked_operation):
    research = _research(_Reader(_snapshot()), _AT)
    research.initialize(("600001",), cancelled=lambda: False)
    expected_status = research.status()
    expected_rows = research.cached(("600001",), include_structured=True)
    entered = threading.Event()
    release = threading.Event()
    qualified = published_company_research._qualified

    def blocked_qualification(*args):
        if threading.current_thread().name.startswith("company-heavy"):
            entered.set()
            assert release.wait(5)
        return qualified(*args)

    def read_publication():
        return research.loaded_identity, research.cached(("600001",), include_structured=True), research.status()

    monkeypatch.setattr(published_company_research, "_qualified", blocked_qualification)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="company-heavy") as heavy, ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="company-reader",
    ) as reader:
        pending = heavy.submit(
            research.status if blocked_operation == "status"
            else lambda: research.cached(("600001",), include_structured=True)
        )
        try:
            assert entered.wait(1)
            identity, rows, status = reader.submit(read_publication).result(timeout=1)
            assert identity == expected_status.loaded_identity
            assert rows == expected_rows
            assert status == expected_status
        finally:
            release.set()
        assert pending.result(timeout=1) == (expected_status if blocked_operation == "status" else expected_rows)
