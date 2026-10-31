from __future__ import annotations

from tests.helpers.configuration import SqlitePolicyFixture

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from scripts.runtime_diagnostics.long_watchlist import audit_code, financial_admission, historical_exclusions
from trader.download.domain.company_evidence import CompanyFinancial, StructuredCompanyPage, StructuredCompanyQuery
from tests.helpers.configuration import (
    CompanyDocumentFilesFixture as CompanyDocumentFiles,
    SQLiteCompanyEvidenceIndexFixture as SQLiteCompanyEvidenceIndex,
)
from trader.infra.settings import LoadStrategySettings
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction
from trader.recommendation.domain.market.eligibility import IssuerEligibilityReason
from trader.recommendation.domain.market.research import FinancialReport

SHANGHAI = ZoneInfo("Asia/Shanghai")
PROJECT_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("profit_field", ("parent_net_profit", "core_net_profit"))
@pytest.mark.parametrize("report_date", (date(2025, 12, 31), date(2026, 6, 30)))
def test_either_latest_report_profit_field_loss_is_excluded(profit_field: str, report_date: date) -> None:
    profits = {"parent_net_profit": 10.0, "core_net_profit": 10.0, profit_field: -1.0}
    report = FinancialReport(report_date, datetime(2026, 8, 20, tzinfo=SHANGHAI), **profits)

    result = financial_admission("000001", (report,), False, date(2026, 10, 9))

    assert result.disposition == "excluded"
    assert result.reasons == ("latest_disclosed_report_loss",)


def test_older_annual_loss_does_not_override_latest_interim_profit() -> None:
    history = (
        FinancialReport(
            date(2024, 12, 31),
            datetime(2025, 4, 20, tzinfo=SHANGHAI),
            parent_net_profit=-10.0,
            core_net_profit=-8.0,
        ),
        FinancialReport(
            date(2025, 12, 31),
            datetime(2026, 4, 20, tzinfo=SHANGHAI),
            parent_net_profit=10.0,
            core_net_profit=8.0,
        ),
        FinancialReport(
            date(2026, 6, 30),
            datetime(2026, 8, 20, tzinfo=SHANGHAI),
            parent_net_profit=3.0,
            core_net_profit=2.0,
        ),
    )

    result = financial_admission("000001", history, True, date(2026, 10, 9))

    assert result.disposition == "financial_pass"
    assert result.reasons == ()


def test_missing_latest_report_does_not_pass_using_an_old_cache_date() -> None:
    annual = FinancialReport(
        date(2025, 12, 31), datetime(2026, 3, 20, tzinfo=SHANGHAI), parent_net_profit=10, core_net_profit=10
    )

    result = financial_admission("000001", (annual,), True, date(2026, 10, 9))

    assert result.disposition == "pending"
    assert "latest_report_missing" in result.reasons


def test_financial_exclusion_is_detected_without_announcement_cache(tmp_path: Path) -> None:
    financial = tmp_path / "company_evidence.sqlite3"
    index = SQLiteCompanyEvidenceIndex(
        financial,
        facts_path=(financial).parent / "company_facts.sqlite3",
        documents=CompanyDocumentFiles((financial).parent / "text"),
    )
    index.initialize()
    now = datetime(2026, 10, 8, 16, tzinfo=SHANGHAI)
    report = CompanyFinancial(
        "000001", date(2025, 12, 31), datetime(2026, 3, 20, tzinfo=SHANGHAI), None, None, None, None, None, None, -1, 10
    )
    index.commit_structured(
        StructuredCompanyQuery("financial", date(2026, 3, 20), now.date()),
        StructuredCompanyPage("financial", 1, 1, 1, financials=(report,)),
        now,
    )
    index.publish(("000001",), now)
    original = financial.read_bytes()

    strategy = LoadStrategySettings(PROJECT_ROOT / "config/strategy.json")
    result = audit_code(
        "000001",
        observed_at=datetime(2026, 10, 9, 16, tzinfo=SHANGHAI),
        policy=strategy.long_research,
        company_policy=strategy.company_scoring,
        timeout=1,
        exclusions={},
        evidence_database=index.facts_path,
        open_dates=(date(2026, 10, 8), date(2026, 10, 9)), sqlite_policy=SqlitePolicyFixture(),
    )

    assert result.disposition == "excluded"
    assert "latest_disclosed_report_loss" in result.reasons
    assert financial.read_bytes() == original
    assert not tuple(tmp_path.rglob("*.json"))


def test_historical_exclusions_ignore_retired_annual_loss_evidence(tmp_path: Path) -> None:
    database = tmp_path / "eligibility.sqlite3"
    with SqliteSession(database) as connection, SqliteTransaction(connection):
        connection.run(
            "create table issuer_eligibility_facts (code text, reason text, effective_at text, evidence_id text)"
        )
        connection.run_many(
            "insert into issuer_eligibility_facts values (?, ?, ?, ?)",
            (
                (
                    "000001",
                    IssuerEligibilityReason.HISTORICAL_AUDITED_LOSS.value,
                    "2025-04-20T23:59:59+08:00",
                    "financial:000001:2024-12-31",
                ),
                (
                    "000002",
                    IssuerEligibilityReason.CONFIRMED_FINANCIAL_FRAUD.value,
                    "2025-04-20T23:59:59+08:00",
                    "announcement:fraud",
                ),
            ),
        )

    exclusions = historical_exclusions((database,), datetime(2026, 10, 9, 16, tzinfo=SHANGHAI), sqlite_policy=SqlitePolicyFixture())

    assert exclusions == {}
