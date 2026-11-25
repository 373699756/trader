"""Exercise supplier/issuer binding, partial admission and exact body dependencies."""

import hashlib
import json
from dataclasses import replace
from datetime import date, timedelta

import pytest

from scripts.download.company.company_field_audit import AuditCompanyFields
from scripts.download.company.company_financial_replay import AuditCompanyFinancialAdoption
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
from trader.download.infra.company_financial_publication import (
    CompanyFinancialAdmissionReceipts,
    PrepareCompanyFinancialAdmissions,
    ReadCompanyFinancialIssuerEvidence,
)
from trader.download.infra.company_financial_observations import WriteCompanyFinancialObservations
from trader.download.infra.company_facts_index import SQLiteCompanyFactsIndex
from trader.download.infra.company_source_schema import COMPANY_SOURCE_SCHEMA
from trader.infra.sqlite_connection import EnsureSqliteSchema, SqliteSession

_CUTOFF = AT + timedelta(days=1)
_PERIOD = date(2026, 9, 30)
_PARENT = "归属于上市公司股东的净利润（元）  80.00  12.00%  -170.00  -16.08%\n"
_ORIGINAL = BODY.replace("归属于上市公司股东的扣除", _PARENT + "归属于上市公司股东的扣除")


def _observation(*, updated_at=AT, notice_at=AT, core=2.0, growth=20.0, net_growth=-16.081055793029):
    report = CompanyFinancial("600001", _PERIOD, notice_at, 0.0, 9.0, growth, net_growth, None, -0.0, -170.0, core)
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
    admitted = ApplyCompanyFinancialAdmission(prior, proof, _CUTOFF)
    assert admitted.financial == old
    assert admitted.latest_financial == proof.report
    assert admitted.latest_financial.parent_net_profit == -170.0
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
        EnsureSqliteSchema(collected, COMPANY_SOURCE_SCHEMA, allow_create=True, exact=True)
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


def test_published_issuer_receipt_traces_filled_value_at_official_time(tmp_path):
    supplier = _observation(net_growth=None)
    document = ReportDocument(_ORIGINAL)
    original = replace(document.disclosure, source="sse", published_at=AT + timedelta(hours=16), digest="a" * 64)
    body_hash = hashlib.sha256(document.text.encode()).digest()
    financial = MatchCompanyFinancialOriginal(supplier, replace(document, disclosure=original), _CUTOFF)
    assert financial is not None and financial.core_profit_growth_pct == 34.0
    revision = CompanyFinancialRevisionProof(original, _PERIOD, _PERIOD, _CUTOFF, supplier.record_hash, b"a" * 32)
    proof = CompanyFinancialAdmission(financial, supplier, body_hash, revision)
    initial = CompileCompanyFacts(CompanyEvidenceInput(
        "600001", AT, AT.date() - timedelta(days=365), AT.date(), "fixture", (), True,
        (), None, (), False, (), False, (),
    ), CompanyEventPolicyFixture())
    previous = replace(financial, book_value_per_share=10.0, core_profit_growth_pct=None, net_profit_growth_pct=None)
    initial = replace(initial, financial=previous, financial_state=CompanyDataState.PARTIAL,
                      latest_financial=previous, published_at=original.published_at)
    admitted = ApplyCompanyFinancialAdmission(initial, proof, _CUTOFF)
    assert admitted.latest_financial.book_value_per_share == 10.0
    policy = SqlitePolicyFixture()
    events = CompanyEventPolicyFixture()
    writer = SQLiteCompanyFactsIndex(tmp_path / "formal.sqlite3", event_policy=events, sqlite_policy=policy)
    writer.initialize()
    writer.publish((admitted,), {original.identity: original.source}, None, _CUTOFF, receipts=CompanyFinancialAdmissionReceipts(
        proof, extraction_hash=writer.extraction_hash, published_at=_CUTOFF,
    ))
    raw_path = tmp_path / "collection.sqlite3"
    payload_hash = b"p" * 32
    with SqliteSession(raw_path, sqlite_policy=policy) as raw, SqliteSession(writer.path, sqlite_policy=policy) as formal:
        EnsureSqliteSchema(raw, COMPANY_SOURCE_SCHEMA, allow_create=True, exact=True)
        raw.run("INSERT INTO disclosures VALUES (1,?,?,?,?,?,?,?,?,1,1)", (
            original.source, original.identity, original.kind, original.title, original.published_at.isoformat(),
            original.url, original.digest, body_hash,
        ))
        raw.run("INSERT INTO disclosure_codes VALUES ('600001',1)")
        raw.run("INSERT INTO document_extractions VALUES (1,?,?,'text','pdftotext',NULL,?)", (
            original.digest, payload_hash, original.published_at.isoformat(),
        ))
        assert ReadCompanyFinancialIssuerEvidence(raw, formal, financial, _CUTOFF, writer.extraction_hash) is None
        WriteCompanyFinancialObservations(raw, (supplier,))
        assert ReadCompanyFinancialIssuerEvidence(raw, formal, financial, AT + timedelta(hours=20), writer.extraction_hash) is None
        assert ReadCompanyFinancialIssuerEvidence(raw, formal, replace(financial, core_profit_growth_pct=35.0),
                                                   _CUTOFF, writer.extraction_hash) is None
        assert ReadCompanyFinancialIssuerEvidence(raw, formal, financial, _CUTOFF, b"x" * 32) is None
        raw.run("UPDATE disclosures SET body_retained=0")
        audit = AuditCompanyFields(formal, raw_path, raw=raw, observed_at=_CUTOFF)
        latest = audit["financial_sources"]["latest"]
        assert latest["official_fields"] == {"numeric": 2}
        assert {item["field"] for item in latest["official_evidence"]} == {"profit_growth_pct", "core_profit_growth_pct"}
        assert all(item["source"] == "sse" for item in latest["official_evidence"])
        assert latest["source_identity_unconfirmed_rows"] == 0 and latest["value_mismatch_rows"] == 0
        assert latest["reasons"] == {"official_value_unverified": 2}

    annotations = tmp_path / "annotations.json"
    annotation = {
        "code": financial.code, "report_date": str(financial.report_date), "disclosed_at": financial.published_at.isoformat(),
        "field": "core_profit_growth_pct", "reported_value": 34.0, "state": "numeric", "source_key": original.identity,
        "record_hash": original.digest, "pdf_sha256": payload_hash.hex(),
    }
    annotations.write_text(json.dumps({"rows": [annotation]}))
    result = AuditCompanyFinancialAdoption(raw_path, writer.path, annotations, observed_at=_CUTOFF,
                                          event_policy=events, sqlite_policy=policy, budget_seconds=30)
    assert result["status"] == "passed" and result["matched"] == 1
    annotations.write_text(json.dumps({"rows": [dict(annotation, report_date="2026-06-30")]}))
    wrong_period = AuditCompanyFinancialAdoption(raw_path, writer.path, annotations, observed_at=_CUTOFF,
                                                event_policy=events, sqlite_policy=policy, budget_seconds=30)
    assert wrong_period["status"] == "degraded" and wrong_period["matched"] == 0
