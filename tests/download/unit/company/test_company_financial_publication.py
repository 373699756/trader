"""Latest disclosed profit evidence is published separately from retained complete financials."""

from dataclasses import replace
from datetime import date, datetime, timedelta
import sqlite3
from zoneinfo import ZoneInfo

import pytest

from tests.helpers.download.company_resources import (
    CompanyDocumentFilesFixture, SQLiteCompanyEvidenceIndexFixture, SQLiteCompanyEvidenceReaderFixture,
    SQLiteCompanyFactsIndexFixture,
)
from tests.helpers.download.company.company_startup import _Reader
from tests.helpers.download.configuration import CompanyEventPolicyFixture
from tests.helpers.recommendation.company_research import PublishedCompanyResearchFixture
from tests.helpers.recommendation.configuration import StrategySettingsFixture
from tests.helpers.shared.configuration import SqlitePolicyFixture
from trader.download.domain.company_events import CompanyDataState
from trader.download.domain.company_evidence import (
    CompanyEvidenceInput, CompanyFinancial, StructuredCompanyPage, StructuredCompanyQuery,
)
from trader.download.domain.company_facts import CompileCompanyFacts
from trader.download.infra.company_facts_schema import COMPANY_FACTS_SCHEMA
from trader.infra.sqlite_connection import SqliteConnectionOptions, SqliteSession
from trader.recommendation.domain.market.research import DeriveLatestDisclosedReportLoss, ResearchComponentsFromObservation
from trader.recommendation.domain.scoring.company import CompanyScoringCalendar
from trader.recommendation.infra.market_data.published_company_research import _observation

_AT = datetime(2026, 11, 8, 12, tzinfo=ZoneInfo("Asia/Shanghai"))


def _reports():
    old = CompanyFinancial("600001", date(2026, 3, 31), _AT - timedelta(days=190), 1, 5, 10, 10, 10, 10, -10, -8)
    latest = replace(
        old, report_date=date(2026, 6, 30), published_at=_AT - timedelta(days=70),
        basic_eps=None, parent_net_profit=3, core_net_profit=2,
    )
    return old, latest


def _source(financials, *, failures=()):
    return CompanyEvidenceInput(
        "600001", _AT, _AT.date() - timedelta(days=365), _AT.date(), "fixture", (), True,
        financials, None, (), False, (), False, failures,
    )


@pytest.mark.parametrize(("old_parent", "old_core", "parent", "core", "expected"), (
    (10, 8, -3, 2, 1.0),
    (-10, -8, 3, 2, 0.0),
    (-10, -8, 3, None, None),
    (10, 8, -3, None, 1.0),
))
def test_latest_partial_report_survives_formal_roundtrip_and_owns_loss_without_cross_period_values(
    tmp_path, old_parent, old_core, parent, core, expected,
):
    old, latest = _reports()
    old = replace(old, parent_net_profit=old_parent, core_net_profit=old_core)
    latest = replace(latest, parent_net_profit=parent, core_net_profit=core)
    facts = CompileCompanyFacts(_source((old, latest)), CompanyEventPolicyFixture())
    assert facts.financial == old and facts.latest_financial == latest
    assert facts.financial_state is CompanyDataState.PARTIAL
    writer = SQLiteCompanyFactsIndexFixture(tmp_path / "company_facts.sqlite3")
    writer.initialize()
    writer.publish((facts,), {}, None, _AT)
    reader = SQLiteCompanyEvidenceReaderFixture(writer.path, raw_path=tmp_path / "absent_raw.sqlite3")
    adopted, = reader.read((facts.code,))
    assert adopted == facts
    assert not (tmp_path / "absent_raw.sqlite3").exists()
    policy = StrategySettingsFixture()
    observation = _observation(
        adopted, _AT, reader.publication_version(), CompanyScoringCalendar(()), policy.company_scoring,
        financial_max_age_days=policy.long_research.financial_max_age_days,
    )
    assert observation.financial.report_date == old.report_date
    assert tuple(report.report_date for report in observation.financial_history) == (old.report_date, latest.report_date)
    assert observation.financial_history[-1].basic_eps is None
    assert observation.financial_history[-1].parent_net_profit == parent
    assert DeriveLatestDisclosedReportLoss(observation, _AT) == expected


def test_latest_profit_evidence_does_not_hide_source_query_failure_or_use_future_report():
    old, latest = _reports()
    facts = CompileCompanyFacts(_source((old, latest), failures=("financial:timeout",)), CompanyEventPolicyFixture())
    policy = StrategySettingsFixture()
    observation = _observation(
        facts, _AT, "fixture", CompanyScoringCalendar(()), policy.company_scoring,
        financial_max_age_days=policy.long_research.financial_max_age_days,
    )
    assert DeriveLatestDisclosedReportLoss(observation, _AT) is None
    future = replace(latest, published_at=_AT + timedelta(days=1))
    current = CompileCompanyFacts(_source((old, future)), CompanyEventPolicyFixture())
    assert current.financial == current.latest_financial == old


def test_previous_partial_latest_report_is_retained_if_next_source_has_no_new_report():
    old, latest = _reports()
    prior = CompileCompanyFacts(_source((old, latest)), CompanyEventPolicyFixture())
    current = CompileCompanyFacts(_source((), failures=("financial:timeout",)), CompanyEventPolicyFixture(), previous=prior)
    assert current.financial == old and current.latest_financial == latest
    assert current.financial_state is CompanyDataState.PARTIAL


def test_latest_financial_values_participate_in_formal_publication_identity(tmp_path):
    old, latest = _reports()
    facts = CompileCompanyFacts(_source((old, latest)), CompanyEventPolicyFixture())
    identities = []
    for name, item in (("original", facts), ("different", replace(
        facts, latest_financial=replace(latest, parent_net_profit=30),
    ))):
        writer = SQLiteCompanyFactsIndexFixture(tmp_path / f"{name}.sqlite3")
        writer.initialize()
        writer.publish((item,), {}, None, _AT)
        identities.append(SQLiteCompanyEvidenceReaderFixture(writer.path).publication_version())
    assert identities[0] != identities[1]


def test_formal_schema_without_latest_report_is_rejected_without_automatic_table_creation(tmp_path):
    path = tmp_path / "old_company_facts.sqlite3"
    with SqliteSession(path, SqliteConnectionOptions(foreign_keys=True), sqlite_policy=SqlitePolicyFixture()) as connection:
        for statement in COMPANY_FACTS_SCHEMA.statements:
            if "CREATE TABLE company_latest_financial" not in statement:
                connection.run(statement)
    original = path.read_bytes()
    with pytest.raises(sqlite3.DatabaseError, match="sqlite_schema_unsupported"):
        SQLiteCompanyFactsIndexFixture(path).initialize()
    assert path.read_bytes() == original
    with SqliteSession(path, SqliteConnectionOptions(read_only=True), sqlite_policy=SqlitePolicyFixture()) as connection:
        assert connection.run("SELECT 1 FROM sqlite_schema WHERE name='company_latest_financial'").fetchone() is None


def test_retained_parsed_financial_cache_can_explicitly_publish_to_another_formal_database(tmp_path):
    source_path, original_path = tmp_path / "collection.sqlite3", tmp_path / "company_facts.sqlite3"
    documents = CompanyDocumentFilesFixture(tmp_path / "documents")
    source = SQLiteCompanyEvidenceIndexFixture(source_path, facts_path=original_path, documents=documents)
    source.initialize()
    old, latest = _reports()
    source.commit_structured(
        StructuredCompanyQuery("financial", old.report_date, _AT.date()),
        StructuredCompanyPage("financial", 1, 1, 2, financials=(old, latest)), _AT,
    )
    source.publish((old.code,), _AT)
    original = original_path.read_bytes()
    replacement_path = tmp_path / "replacement_company_facts.sqlite3"
    SQLiteCompanyFactsIndexFixture(replacement_path).initialize()
    replacement = SQLiteCompanyEvidenceIndexFixture(
        source_path, facts_path=replacement_path, documents=documents,
    )
    replacement.initialize()
    replacement.publish((old.code,), _AT)
    adopted, = SQLiteCompanyEvidenceReaderFixture(replacement_path).read((old.code,))
    assert adopted.financial == old and adopted.latest_financial == latest
    assert original_path.read_bytes() == original


def test_financial_age_is_requalified_without_reload_and_does_not_expire_latest_loss():
    old, _ = _reports()
    report = replace(old, report_date=date(2024, 6, 30), published_at=_AT - timedelta(days=550))
    reader = _Reader(_source((report,)))
    research = PublishedCompanyResearchFixture(
        reader, wall_clock=lambda: _AT,
        open_dates=lambda: (_AT.date() - timedelta(days=1), _AT.date(), _AT.date() + timedelta(days=1)),
        monotonic=lambda: 0.0,
    )
    research.initialize((report.code,), cancelled=lambda: False)
    initial = research.cached((report.code,), include_structured=True, as_of=_AT)[report.code]
    later_at = _AT + timedelta(days=1)
    later = research.cached((report.code,), include_structured=True, as_of=later_at)[report.code]
    assert initial.financial_history_complete
    assert ResearchComponentsFromObservation(initial).financial == "known_clear"
    assert not later.financial_history_complete
    assert ResearchComponentsFromObservation(later).financial == "stale"
    assert "financial_scoring:report_stale" in later.source_errors
    assert initial.financial_history == later.financial_history
    assert DeriveLatestDisclosedReportLoss(later, later_at) == 1
    assert reader.visits == 1
