"""Exercise source review against the same persisted inputs used by company sync."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from scripts.download.company.company_field_audit import AuditCompanyFields
from scripts.download.company.company_source_qualification import AuditCompanySourceQualification, ProjectCompanySourceQualification
from tests.download.component.company.test_company_normalized_round import _CUTOFF, _NOTICE, _observation, _page
from tests.helpers.download.company.company_evidence import _disclosure, _extraction
from tests.helpers.download.company.company_financial_report import BODY
from tests.helpers.download.configuration import CompanyEventPolicyFixture
from tests.helpers.shared.configuration import SqlitePolicyFixture
from trader.download.domain.company_evidence import CompanyRatio, StructuredCompanyPage, StructuredCompanyQuery
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
from trader.download.infra.company_sync_inputs import SQLiteCompanySyncInputStore
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


def _inputs(tmp_path: Path):
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
    inputs = SQLiteCompanySyncInputStore(paths.company_sources, sqlite_policy=sqlite_policy)
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
    financial = StructuredCompanyQuery("financial", oldest, today)
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


def _audit(paths, documents, unlock_days):
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
    )
    assert report["status"] == "degraded"
    return ProjectCompanySourceQualification(report["summary"]["source_qualification_audit"])


def test_source_qualification_reads_original_candidates_without_admitting_observations(tmp_path):
    paths, documents, unlock_days = _inputs(tmp_path)
    databases = (paths.company, paths.company_sync, paths.company_sources)
    before = [path.read_bytes() for path in databases]
    audit = _audit(paths, documents, unlock_days)
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
