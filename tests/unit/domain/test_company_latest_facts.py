"""Dated snapshots cannot regress when an older source finishes later."""

from dataclasses import replace
from datetime import timedelta

from tests.component import test_company_evidence as fixtures
from trader.download.domain.company_events import CompanyDataState
from trader.download.domain.company_evidence import CompanyEvidenceInput, CompanyRatio
from trader.download.domain.company_facts import CompileCompanyFacts


def test_late_older_pledge_keeps_newer_value_and_reports_incomplete_source():
    now = fixtures.NOW
    latest = CompanyRatio("600001", now.date(), 2.0)
    source = CompanyEvidenceInput(
        "600001", now, now.date() - timedelta(days=365), now.date(), "test", (), True,
        (), latest, (), False, (), False, (),
    )
    previous = CompileCompanyFacts(source, fixtures.CompanyEventPolicy())
    older = CompanyRatio("600001", now.date() - timedelta(days=1), 8.0)
    current = CompileCompanyFacts(replace(source, pledge=older), fixtures.CompanyEventPolicy(), previous=previous)
    assert current.pledge == latest
    assert current.pledge_state is CompanyDataState.PARTIAL


def test_future_pledge_cannot_replace_latest_effective_snapshot():
    now = fixtures.NOW
    latest = CompanyRatio("600001", now.date(), 2.0)
    source = CompanyEvidenceInput(
        "600001", now, now.date() - timedelta(days=365), now.date(), "test", (), True,
        (), latest, (), False, (), False, (),
    )
    previous = CompileCompanyFacts(source, fixtures.CompanyEventPolicy())
    future = CompanyRatio("600001", now.date() + timedelta(days=1), 0.0)
    current = CompileCompanyFacts(replace(source, pledge=future), fixtures.CompanyEventPolicy(), previous=previous)
    assert current.pledge == latest
    assert current.pledge_state is CompanyDataState.PARTIAL
