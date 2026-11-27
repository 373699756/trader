"""Exercise source review against the same persisted inputs used by company sync."""

from __future__ import annotations

import json
from io import BytesIO
from datetime import timedelta
from pathlib import Path

import pytest
from openpyxl import load_workbook

from scripts.download.company import company_source_qualification
from scripts.download.company.company_field_coverage import ReadCompanyFieldReport
from scripts.download.company.company_field_workbook import WriteCompanyFieldWorkbook
from scripts.download.company.company_field_audit import AuditCompanyFields
from scripts.download.company.company_source_qualification import AuditCompanySourceQualification, ProjectCompanySourceQualification
from scripts.download.company.company_stock_fields import StockFieldReport
from tests.helpers.recommendation.configuration import CompanyScoringPolicyFixture
from tests.download.component.company.test_company_normalized_round import _CUTOFF, _NOTICE, _observation, _page
from tests.helpers.download.company.company_evidence import _disclosure, _extraction
from tests.helpers.download.company.company_financial_report import BODY
from tests.helpers.download.configuration import CompanyEventPolicyFixture
from tests.helpers.shared.configuration import SqlitePolicyFixture
from trader.download.domain.company_evidence import CompanyLatestFinancialQuery, CompanyRatio, StructuredCompanyPage, StructuredCompanyQuery
from trader.download.domain.company_sync import (
    CompanyFinancialRequestIdentity,
    CompanySyncFamily,
    CompanySyncPhasePlan,
    CompanySyncRoundScope,
)
from trader.download.infra.company_document_files import CompanyDocumentFiles
from trader.download.infra.company_facts_index import SQLiteCompanyFactsIndex
from trader.download.infra.company_facts_protocol import EncodeCompanyDate
from trader.download.infra.company_material_index import SQLiteCompanyMaterialIndex
from trader.download.infra.company_normalized_index import SQLiteCompanyNormalizedIndex
from trader.download.infra.company_sync_inputs import SQLiteCompanySyncInputs
from trader.download.infra.company_sync_journal import SQLiteCompanySyncRoundJournal
from trader.download.infra.company_sync_runtime import CompanySyncRuntime
from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.infra.settings.data_paths import DataPaths
from trader.infra.sqlite_connection import SqliteConnectionOptions, SqliteSession
from trader.recommendation.domain.market.research import LongResearchPolicy

_CODES = ("600001", "600002")


class _UnusedSource:
    def __getattr__(self, operation):
        raise AssertionError(f"unexpected_source_call:{operation}")


def _inputs(tmp_path: Path, *, latest: bool = False):
    paths = DataPaths(tmp_path)
    paths.company_sources.parent.mkdir(parents=True)
    sqlite_policy = SqlitePolicyFixture()
    policy = CompanyEventPolicyFixture()
    facts = SQLiteCompanyFactsIndex(paths.company, event_policy=policy, sqlite_policy=sqlite_policy)
    documents = CompanyDocumentFiles(
        paths.company_documents, max_text_characters=100_000, max_bytes=1_000_000, reserve_bytes=1,
    )
    material = SQLiteCompanyMaterialIndex(
        paths.company_sources,
        facts=facts,
        documents=documents,
        event_policy=policy,
        sqlite_policy=sqlite_policy,
    )
    normalized = SQLiteCompanyNormalizedIndex(
        paths.company_sources,
        facts,
        event_policy=policy,
        sqlite_policy=sqlite_policy,
        clean_material=lambda: None,
    )
    journal = SQLiteCompanySyncRoundJournal(
        paths.company_sync, sqlite_policy=sqlite_policy, publication_identity=lambda: "",
    )
    inputs = SQLiteCompanySyncInputs(paths.company_sources, sqlite_policy=sqlite_policy)
    source = _UnusedSource()
    runtime = CompanySyncRuntime(
        normalized,
        facts,
        material,
        journal,
        inputs,
        event_policy=policy,
        sqlite_policy=sqlite_policy,
        read_document=documents.read,
        financial_source=source,
        ratio_source=source,
        annual_source=source,
        official_source=source,
    )
    runtime.initialize(_CODES, _CUTOFF)
    scope = CompanySyncRoundScope(_CODES, _CUTOFF)
    today = _CUTOFF.date()
    oldest = today - timedelta(days=365)
    financial = CompanyLatestFinancialQuery("financial", oldest, today) if latest else StructuredCompanyQuery("financial", oldest, today)
    pledge = StructuredCompanyQuery("pledge", oldest, today)
    unlock = StructuredCompanyQuery("unlock", today, today + timedelta(days=policy.unlock_days))
    source_plans = {
        CompanySyncFamily.FINANCIAL: CompanyFinancialRequestIdentity(financial, "observations"),
        CompanySyncFamily.PLEDGE: CanonicalDigest(pledge),
        CompanySyncFamily.UNLOCK: CanonicalDigest(unlock),
    }
    journal.begin(scope, tuple(
        CompanySyncPhasePlan(family, source_plans.get(family, CanonicalDigest(family.value)))
        for family in scope.families
    ), max_source_calls=12)
    normalized.accept(
        financial, _page(_observation(eps=None), _observation(code=_CODES[1], parent=-100.0)), _CODES, _CUTOFF,
    )
    pledge_day = today - timedelta(days=1)
    inputs.freeze_pledge_day(scope.identity, pledge_day, _CUTOFF)
    inputs.accept_ratios(
        scope.identity,
        StructuredCompanyQuery("pledge", pledge_day, pledge_day),
        StructuredCompanyPage("pledge", 1, 1, 1, ratios=(CompanyRatio(_CODES[0], pledge_day, 15.0),)),
        _CUTOFF,
    )
    inputs.accept_ratios(
        scope.identity,
        unlock,
        StructuredCompanyPage("unlock", 1, 1, 1, ratios=(CompanyRatio(_CODES[1], today + timedelta(days=1), 1.5),)),
        _CUTOFF,
    )
    original = _disclosure("original", "2026年第三季度报告", when=_NOTICE)
    unrelated = _disclosure("other-period", "2025年第三季度报告", when=_NOTICE)
    notice = _disclosure("notice", "2026年第三季度报告提示性公告", when=_NOTICE)
    revision = _disclosure("revision", "关于2026年第三季度报告的更正", when=_NOTICE + timedelta(days=2))
    material.register_disclosures((original, unrelated, notice, revision))
    material.commit_document(original, _extraction(BODY), _CUTOFF)
    return paths, documents, policy.unlock_days


def _audit(paths, documents, unlock_days, *, stock_fields=None):
    report = AuditCompanySourceQualification(
        paths,
        documents=documents,
        sqlite_policy=SqlitePolicyFixture(),
        policy=LongResearchPolicy(
            550, 10.0, 50.0, 1.0, 8.0, 2.0, 10.0, 2.5, -10.0, -20.0, -20.0,
            (10.0, 20.0, 35.0), (1.0, 5.0, 10.0),
        ),
        unlock_days=unlock_days,
        deadline=10.0,
        monotonic=lambda: 0.0,
        stock_fields=stock_fields,
    )
    assert report["status"] == "degraded"
    return ProjectCompanySourceQualification(report["summary"]["source_qualification_audit"])


@pytest.mark.parametrize("latest", (False, True))
def test_field_workbook_uses_frozen_population_and_keeps_formal_and_source_separate(tmp_path, latest):
    paths, _, _ = _inputs(tmp_path, latest=latest)
    databases = (paths.company_sync, paths.company_sources, paths.company)
    before = [path.read_bytes() for path in databases]
    report = ReadCompanyFieldReport(paths, _CUTOFF)
    assert report.population == 2
    assert report.missing_financial == ((_CODES[0], ("每股收益（EPS）",)),)
    assert next(m.present for m in report.metrics if m.category == "D25正式读取") == 0
    with BytesIO() as stream:
        WriteCompanyFieldWorkbook(stream, report)
        stream.seek(0)
        book = load_workbook(stream, read_only=True)
        try:
            eps = next(row for row in book["来源与正式覆盖"].values if row[1] == "每股收益（EPS）")
            assert eps[2:6] == (1, 1, 2, 0.5)
            contract = next(row for row in book["事件字段适用范围"].values if row[0] == "合同金额")
            assert contract[1] == 0 and contract[7] is None
            assert book["正式库全部物理列"].max_row == len(report.columns) + 1
        finally:
            book.close()
    assert before == [path.read_bytes() for path in databases]


@pytest.mark.parametrize("latest", (False, True))
def test_source_qualification_reads_original_candidates_without_admitting_observations(tmp_path, latest):
    paths, documents, unlock_days = _inputs(tmp_path, latest=latest)
    databases = (paths.company, paths.company_sync, paths.company_sources)
    before = [path.read_bytes() for path in databases]
    audit = _audit(paths, documents, unlock_days)
    assert audit["round_active"]
    if latest:
        assert audit["financial"]["pages"]["market_prefix"] == {
            "read_pages": 1, "declared_pages": 1, "read_rows": 2, "declared_rows": 2, "exact_issuer_count": 0,
        }
    assert audit["network_requests"] == audit["source_data_writes"] == 0
    assert audit["source_admission_verified"] is audit["production_adopted"] is False
    assert audit["financial"]["latest_incomplete_stocks"] == 1
    assert audit["financial"]["sample"]["loss_only"] == [_CODES[1]]
    first = audit["financial"]["review"][0]
    assert first["missing_fields"] == ["basic_eps"]
    original, = first["original_candidates"]
    assert original["source_key"] == "original" and original["body_status"] == "exact_retained_text"
    assert original["source_null_numeric_fields"] == ["basic_eps"]
    assert "roe_pct" in original["conflicting_fields"]
    assert [row["source_key"] for row in first["revision_candidates"]] == ["revision"]
    assert audit["pledge"]["pages"]["closed"] is audit["unlock"]["pages"]["closed"] is True
    assert audit["pledge"]["without_observation"] == audit["unlock"]["without_observation"] == 1
    assert audit["financial"]["published_complete_stocks"] == audit["pledge"]["published_complete_stocks"] == audit["unlock"]["published_complete_stocks"] == 0
    assert audit["unlock"]["sample_observations"][0]["entries"] == [
        {"date": (_CUTOFF.date() + timedelta(days=1)).isoformat(), "ratio_pct": 1.5}
    ]
    assert "same_basis_total_shares" in audit["pledge"]["required_evidence"]
    assert "market_and_window_coverage" in audit["unlock"]["required_evidence"]
    assert [path.read_bytes() for path in databases] == before


def test_source_qualification_refuses_uncheckpointed_inputs(tmp_path):
    paths, documents, unlock_days = _inputs(tmp_path)
    paths.company_sources.with_name("company_sources.sqlite3-wal").write_bytes(b"pending transaction")
    with pytest.raises(ValueError, match="company_source_qualification_uncheckpointed_database"):
        _audit(paths, documents, unlock_days)


def test_stock_fields_keeps_supplier_nulls_and_readable_scoring_separate(tmp_path, monkeypatch):
    paths, documents, unlock_days = _inputs(tmp_path)
    policy = CompanyEventPolicyFixture()
    options = SqlitePolicyFixture()
    facts = SQLiteCompanyFactsIndex(paths.company, event_policy=policy, sqlite_policy=options)
    normalized = SQLiteCompanyNormalizedIndex(paths.company_sources, facts, event_policy=policy, sqlite_policy=options,
                                               clean_material=lambda: None)
    visible_at = _CUTOFF + timedelta(minutes=15)
    query = StructuredCompanyQuery("financial", _CUTOFF.date() - timedelta(days=365), _CUTOFF.date())
    assert normalized.publish_financial_scoring(query, _CODES, _CUTOFF, eligible_codes=_CODES, available_at=visible_at)
    details = StockFieldReport(tmp_path / "stocks.json", visible_at, CompanyScoringPolicyFixture(events=policy))
    build_report = company_source_qualification.BuildStockFieldReport

    def advance_live_writer(*args, **kwargs):
        with SqliteSession(paths.company_sources, sqlite_policy=options) as sources:
            sources.run("UPDATE company_ratio_observation SET ratio=25 WHERE component='pledge'")
        return build_report(*args, **kwargs)

    monkeypatch.setattr(company_source_qualification, "BuildStockFieldReport", advance_live_writer)
    result = _audit(paths, documents, unlock_days, stock_fields=details)
    assert result["stock_field_rows"] == 2
    report = json.loads(details.output.read_text())
    assert report["reader_at_source_cutoff"] == 0
    assert report["reader_at_audit"] == 2
    assert report["decision_or_seat_effect"] == "unmeasured"
    one, two = report["stocks"]
    assert one["latest_in_supplier_batch"]["source_null_fields"] == ["basic_eps"]
    assert one["formal"]["latest_financial"] is None
    assert one["financial_field_comparison"]["source_null"] == ["basic_eps"]
    assert len(one["financial_field_comparison"]["formal_report_missing"]) == 8
    assert one["reader_matches_source_record"] is True
    assert one["d25_reader_at_audit"]["available_at"] == visible_at.isoformat()
    assert one["source_pledges"] and not two["source_pledges"]
    assert one["source_pledges"][0]["ratio_pct"] == 15.0
    assert report["source_windows"]["pledge"]["through"] == one["source_pledges"][0]["date"]
    assert not one["source_unlocks"] and two["source_unlocks"]
    with pytest.raises(FileExistsError):
        _audit(paths, documents, unlock_days, stock_fields=details)


def test_audit_reads_closed_round_and_reobserved_unchanged_source_content(tmp_path):
    paths, documents, unlock_days = _inputs(tmp_path, latest=True)
    policy = SqlitePolicyFixture()
    with SqliteSession(paths.company_sync, sqlite_policy=policy) as journal:
        journal.run("UPDATE company_sync_round SET active=0")
    with SqliteSession(paths.company_sources, sqlite_policy=policy) as inputs:
        inputs.run("UPDATE company_financial_observation SET observed_at=?", ((_CUTOFF - timedelta(hours=1)).isoformat(),))
    audit = _audit(paths, documents, unlock_days)
    assert not audit["round_active"]
    assert audit["financial"]["pages"]["closed"] and audit["financial"]["observed_stocks"] == 2


def test_field_audit_compares_source_observations_without_certifying_ratio_coverage(tmp_path):
    paths, _, _ = _inputs(tmp_path)
    policy = SqlitePolicyFixture()
    pledge_day = _CUTOFF.date() - timedelta(days=1)
    unlock_day = _CUTOFF.date() + timedelta(days=1)
    with (
        SqliteSession(paths.company_sources, sqlite_policy=policy) as sources,
        SqliteSession(paths.company, sqlite_policy=policy) as formal,
    ):
        sources.run("INSERT INTO company_ratio_observation VALUES(?,?,?,?,?)",
            ("prior-scope", "pledge", _CODES[0], pledge_day.isoformat(), 15.0))
        sources.run("UPDATE company_ratio_observation SET ratio=1.503 WHERE component='unlock'")
        formal.run("UPDATE company_facts SET pledge_pct=15.0,pledge_date=? WHERE code=?",
            (EncodeCompanyDate(pledge_day), _CODES[0]))
        formal.run("UPDATE company_facts SET pledge_pct=0,pledge_date=?,unlock_state=1 WHERE code=?",
            (EncodeCompanyDate(pledge_day), _CODES[1]))
        formal.run("INSERT INTO company_unlock VALUES(?,?,?)", (_CODES[1], EncodeCompanyDate(unlock_day), 1.5))
    options = SqliteConnectionOptions(read_only=True)
    with (
        SqliteSession(paths.company_sources, options, sqlite_policy=policy) as sources,
        SqliteSession(paths.company, options, sqlite_policy=policy) as formal,
    ):
        audit = AuditCompanyFields(formal, paths.company_sources, raw=sources, observed_at=_CUTOFF)
    agreement = audit["source_agreement"]
    assert (agreement["pledge_rows"], agreement["pledge_observation_matches"], agreement["pledge_without_matching_observation"]) == (2, 1, 1)
    assert (agreement["unlock_rows"], agreement["unlock_observation_matches"], agreement["unlock_without_matching_observation"]) == (1, 0, 1)
    assert "company_field_integrity_failed" not in audit["findings"]
