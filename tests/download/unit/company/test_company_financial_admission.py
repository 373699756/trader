"""Exercise supplier/issuer binding, partial admission and exact body dependencies."""

import hashlib
from dataclasses import replace
from datetime import date, timedelta

import pytest

from tests.helpers.download.company.company_financial_report import AT, BODY, ReportDocument
from tests.helpers.download.configuration import CompanyEventPolicyFixture
from tests.helpers.shared.configuration import SqlitePolicyFixture
from trader.download.domain.company_evidence import CompanyEvidenceInput, CompanyFinancial
from trader.download.domain.company_events import CompanyDataState
from trader.download.domain.company_facts import CompileCompanyFacts
from trader.download.domain.company_financial_admission import (
    CompanyFinancialAdmission,
    CompanyFinancialRevisionProof,
    MatchCompanyFinancialOriginal,
)
from trader.download.domain.company_normalization import ApplyCompanyFinancialAdmission
from trader.download.domain.company_source_records import (
    CompanyFinancialMissingField,
    CompanyFinancialSourceObservation,
    CompanyFinancialSourceRecordHash,
    CompanyFinancialSourceValues,
)
from trader.download.infra.company_evidence_sql import SCHEMA
from trader.download.infra.company_financial_publication import (
    CompanyFinancialAdmissionReceipts,
    PrepareCompanyFinancialAdmissions,
)
from trader.infra.sqlite_connection import EnsureSqliteSchema, SqliteSession

_CUTOFF = AT + timedelta(days=1)
_PERIOD = date(2026, 9, 30)
_PARENT = "归属于上市公司股东的净利润（元）  80.00  12.00%  -170.00  -16.08%\n"
_ORIGINAL = BODY.replace("归属于上市公司股东的扣除", _PARENT + "归属于上市公司股东的扣除")


def _observation(*, updated_at=AT, notice_at=AT, core=2.0, growth=20.0):
    report = CompanyFinancial("600001", _PERIOD, notice_at, 0.0, 9.0, growth, -16.081055793029, None, -0.0, -170.0, core)
    identity = "600001.SH"
    return CompanyFinancialSourceObservation(
        report, identity, notice_at, updated_at, _CUTOFF, "CNY",
        CompanyFinancialSourceRecordHash(report, identity, notice_at, updated_at, "CNY"),
        tuple(CompanyFinancialMissingField(field) for field, value in CompanyFinancialSourceValues(report) if value is None),
    )


def test_admission_binds_ytd_profits_and_only_independently_proven_ratios():
    supplier = _observation()
    report = MatchCompanyFinancialOriginal(supplier, ReportDocument(_ORIGINAL), _CUTOFF)
    assert report is not None
    assert (report.report_date, report.published_at) == (_PERIOD, AT)
    assert (report.parent_net_profit, report.core_net_profit) == (-170.0, 2.0)
    assert report.net_profit_growth_pct == supplier.financial.net_profit_growth_pct
    assert report.core_profit_growth_pct == 34.0
    assert report.basic_eps == 0.0
    assert report.roe_pct == 0.0
    assert report.revenue_growth_pct is None  # supplier's 20 disagrees with the issuer's 21
    assert report.book_value_per_share is None  # issuer table has no matching cell
    assert supplier.financial.core_profit_growth_pct is None
    assert MatchCompanyFinancialOriginal(supplier, replace(ReportDocument(_ORIGINAL), checked=False), _CUTOFF) is None

    missing_unit = MatchCompanyFinancialOriginal(supplier, ReportDocument(_ORIGINAL.replace("-16.08%", "-16.08")), _CUTOFF)
    assert missing_unit is not None and missing_unit.net_profit_growth_pct is None


@pytest.mark.parametrize("case", ("wrong_period_column", "wrong_day", "later_update", "no_update", "wrong_units", "issuer_mismatch"))
def test_admission_refuses_ambiguous_source_or_report(case):
    body = _ORIGINAL
    supplier = _observation()
    if case == "wrong_period_column":
        supplier = _observation(core=1.0)
    if case == "wrong_day":
        supplier = _observation(notice_at=AT - timedelta(days=1))
    if case == "later_update":
        supplier = _observation(updated_at=_CUTOFF)
    if case == "no_update":
        supplier = _observation(updated_at=None)
    if case == "wrong_units":
        body = body.replace("单位：元 币种：人民币", "单位：万元 币种：人民币")
    if case == "issuer_mismatch":
        body = body.replace("证券代码：600001", "证券代码：600002")
    assert MatchCompanyFinancialOriginal(supplier, ReportDocument(body), _CUTOFF) is None


def test_latest_release_can_fill_partial_without_degrading_previous_complete_score_basis():
    supplier = _observation()
    document = ReportDocument(_ORIGINAL)
    report = MatchCompanyFinancialOriginal(supplier, document, _CUTOFF)
    assert report is not None
    original = replace(document.disclosure, digest="a" * 64)
    revision = CompanyFinancialRevisionProof(original, _PERIOD, _PERIOD, _CUTOFF, supplier.record_hash, b"a" * 32)
    proof = CompanyFinancialAdmission(report, supplier, hashlib.sha256(_ORIGINAL.encode()).digest(), revision)
    old = replace(supplier.financial, report_date=date(2026, 6, 30), published_at=AT - timedelta(days=60), core_profit_growth_pct=2)
    evidence = CompanyEvidenceInput(
        "600001", AT, AT.date() - timedelta(days=365), AT.date(), "fixture", (), True,
        (old,), None, (), False, (), False, (),
    )
    prior = CompileCompanyFacts(evidence, CompanyEventPolicyFixture())
    assert prior.financial_state is CompanyDataState.COMPLETE
    assert ApplyCompanyFinancialAdmission(prior, proof, _CUTOFF) is prior

    prior = replace(prior, financial_state=CompanyDataState.PARTIAL)
    admitted = ApplyCompanyFinancialAdmission(prior, proof, _CUTOFF)
    assert admitted.financial == old
    assert admitted.latest_financial == proof.report
    assert admitted.financial_state is CompanyDataState.PARTIAL
    assert admitted.risk_events == prior.risk_events
    assert ApplyCompanyFinancialAdmission(admitted, proof, _CUTOFF) is admitted
    conflicting = replace(admitted, latest_financial=replace(proof.report, parent_net_profit=100.0))
    assert ApplyCompanyFinancialAdmission(conflicting, proof, _CUTOFF) is conflicting


def test_extraction_qualified_issuer_body_requires_closed_directory_and_no_correction(tmp_path):
    supplier = _observation()
    original = replace(ReportDocument(_ORIGINAL).disclosure, digest="a" * 64)
    body_hash = hashlib.sha256(_ORIGINAL.encode()).digest()
    revision = CompanyFinancialRevisionProof(original, _PERIOD, _PERIOD, _CUTOFF, supplier.record_hash, b"a" * 32)
    args = (original.source, original.identity, original.kind, original.title, AT.isoformat(), original.url, original.digest, body_hash)
    with SqliteSession(tmp_path / "collection.sqlite3", sqlite_policy=SqlitePolicyFixture()) as collected:
        EnsureSqliteSchema(collected, SCHEMA, allow_create=True, exact=True)
        collected.run("INSERT INTO disclosures VALUES (1,?,?,?,?,?,?,?,?,1,1)", args)
        collected.run("INSERT INTO disclosure_codes VALUES ('600001',1)")
        collected.run("INSERT INTO document_extractions VALUES (1,?,?,'text','pdftotext',NULL,?)", (original.digest, b"p" * 32, AT.isoformat()))

        def prepare(read_body=lambda digest: _ORIGINAL, *, revisions=()):
            return PrepareCompanyFinancialAdmissions(collected, (supplier,), _CUTOFF,
                read_document=read_body, check=lambda: None, revision_proofs=revisions)

        assert prepare() == ()
        collected.run("INSERT INTO coverage VALUES ('sse','2026-10-01',?,?)", (_CUTOFF.date().isoformat(), _CUTOFF.isoformat()))
        assert prepare() == ()
        assert prepare(revisions=(replace(revision, source_record_hash="b" * 64),)) == ()
        assert prepare(revisions=(replace(revision, checked_through=_CUTOFF - timedelta(hours=1)),)) == ()
        proof, = prepare(revisions=(revision,))
        assert proof.body_hash == body_hash
        valid, attempt = CompanyFinancialAdmissionReceipts(proof, extraction_hash=b"e" * 32, published_at=_CUTOFF)
        assert valid.record_hash == original.digest and valid.body_hash == body_hash
        assert attempt.policy_id.startswith(f"financial_issuer_original:{_PERIOD}:{supplier.record_hash}:")
        assert prepare(lambda digest: "wrong document", revisions=(revision,)) == ()

        collected.run("INSERT INTO disclosures VALUES (2,'cninfo','second','announcement','撤回财务报表的公告',?,'','x',NULL,0,0)", (_CUTOFF.isoformat(),))
        collected.run("INSERT INTO disclosure_codes VALUES ('600001',2)")
        assert prepare(revisions=(revision,)) == ()
