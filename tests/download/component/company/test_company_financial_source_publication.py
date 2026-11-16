"""Official financial cells publish through the normal owner and survive source-null refresh."""

from dataclasses import replace
from datetime import date, timedelta
import json

import pytest

from tests.helpers.download.company import company_evidence as fixtures
from tests.helpers.download.company.company_financial_report import BODY, AT, ReportDocument
from tests.helpers.download.company_resources import SQLiteCompanyEvidenceReaderFixture
from scripts.download.company.company_query_audit import AuditCompanyDatabase
from scripts.download.company.company_financial_replay import AuditCompanyFinancialAdoption
from tests.helpers.shared.configuration import SqlitePolicyFixture
from trader.download.application.company_evidence import CompanyEvidenceDependencies
from trader.download.domain.company_evidence import CompanyFinancial, DisclosurePage, DisclosurePartition, StructuredCompanyQuery, StructuredCompanyPage
from trader.download.domain.company_financial_report import CompanyFinancialReportRequest
from trader.download.infra.company_facts_index import ReadCompanyPublicationIdentity
from trader.infra.sqlite_connection import SqliteSession

NOW = AT + timedelta(days=17)
_RAW = CompanyFinancial("600001", date(2026, 9, 30), AT, None, 5, None, 3, None, None, 100, 90)


def _structured(index, report=_RAW):
    index.commit_structured(
        StructuredCompanyQuery("financial", date(2026, 1, 1), NOW.date()),
        StructuredCompanyPage("financial", 1, 1, 1, financials=(report,)), NOW,
    )


def _setup(tmp_path, body=BODY):
    index = fixtures._index(tmp_path)
    _structured(index)
    index.publish(("600001",), NOW)
    request, = index.report_input_plan(("600001",), NOW).financial_reports
    assert request == CompanyFinancialReportRequest("600001", date(2026, 9, 30), AT)
    document = ReportDocument(body)
    index.commit_financial_reports(request, (document.disclosure,), NOW)
    assert index.commit_document(document.disclosure, fixtures._extraction(body), NOW)
    return index, document


def _latest(index):
    return SQLiteCompanyEvidenceReaderFixture(index.facts_path).read(("600001",))[0].latest_financial


def test_normal_publication_adopts_official_cells_and_reuses_their_exact_receipts_after_null_refresh(tmp_path, monkeypatch):
    index, document = _setup(tmp_path)
    index.publish(("600001",), NOW)
    latest = _latest(index)
    assert latest == replace(_RAW, basic_eps=0.0, roe_pct=0.0, core_profit_growth_pct=34.0, revenue_growth_pct=21.0)
    with SqliteSession(index.path) as collected:
        assert tuple(collected.run("SELECT eps,roe_pct,core_profit_growth_pct,revenue_growth_pct FROM financials").require_one()) == (None,) * 4
        assert collected.run("SELECT COUNT(*) FROM company_material_dependencies WHERE scope='financial'").require_one()[0] == 1
    with SqliteSession(index.facts_path) as formal:
        before = ReadCompanyPublicationIdentity(formal)
        receipts = tuple(tuple(row) for row in formal.run("SELECT * FROM company_input_receipt ORDER BY outcome,policy_id"))
        assert sum(row[-2] == "field_attempted" for row in receipts) == 4
    restarted = fixtures._index(tmp_path)
    _structured(restarted)

    def unexpected_read(*args, **kwargs):
        raise AssertionError("unchanged official quarterly proof must not re-read its body")

    monkeypatch.setattr(restarted._documents, "read", unexpected_read)
    restarted.prepare_and_publish(("600001",), NOW)
    assert _latest(restarted) == latest
    with SqliteSession(index.facts_path) as formal:
        assert ReadCompanyPublicationIdentity(formal) == before
        assert tuple(tuple(row) for row in formal.run("SELECT * FROM company_input_receipt ORDER BY outcome,policy_id")) == receipts
    audit = AuditCompanyDatabase(index.path, facts_path=index.facts_path, now=NOW, budget_seconds=30,
                                sqlite_policy=SqlitePolicyFixture(), event_policy=fixtures.CompanyEventPolicy())
    financial = audit["summary"]["database"]["publication"]["field_audit"]["financial_sources"]["latest"]
    assert financial["value_mismatch_rows"] == 0 and financial["provider_null_fields"] == 4
    assert financial["official_fields"] == {"numeric": 4} and financial["missing_fields"] == {}
    assert all(item["source_keys"] == [document.disclosure.identity] for item in financial["official_evidence"])


@pytest.mark.parametrize("case", ("wrong_code", "wrong_year", "conflicting_source", "late_revision", "missing_body"))
def test_unbound_or_conflicting_official_material_cannot_fill_a_financial_source_null(tmp_path, case):
    body = BODY.replace("600001", "600002") if case == "wrong_code" else BODY.replace("2026年第三季度报告", "2025年第三季度报告") if case == "wrong_year" else BODY
    index, document = _setup(tmp_path, body)
    if case in {"conflicting_source", "late_revision"}:
        item = replace(document.disclosure, identity="other", digest="other", published_at=AT + timedelta(days=1) if case == "late_revision" else AT)
        index.commit_financial_reports(CompanyFinancialReportRequest("600001", date(2026, 9, 30), AT), (item,), NOW)
        index.commit_document(item, fixtures._extraction(BODY.replace("34.00%", "35.00%")), NOW)
    if case == "missing_body":
        index._documents.release(frozenset())
    if case == "missing_body":
        with pytest.raises(FileNotFoundError):
            index.publish(("600001",), NOW)
    else:
        index.publish(("600001",), NOW)
        expected = replace(_RAW, basic_eps=0.0, roe_pct=0.0, revenue_growth_pct=21.0) if case == "conflicting_source" else _RAW
        assert _latest(index) == expected


@pytest.mark.parametrize("raw,state", (("不适用", "not_applicable"), ("--", "unquantified")))
def test_nonnumeric_official_cell_stays_null_with_a_distinct_published_source_reason(tmp_path, raw, state):
    index, _ = _setup(tmp_path, BODY.replace("0.0000", raw))
    index.publish(("600001",), NOW)
    assert _latest(index).basic_eps is None
    audit = AuditCompanyDatabase(index.path, facts_path=index.facts_path, now=NOW, budget_seconds=30, sqlite_policy=SqlitePolicyFixture())
    group = audit["summary"]["database"]["publication"]["field_audit"]["financial_sources"]["latest"]
    assert group["reasons"] == {"official_" + state: 1}
    assert group["official_fields"] == {"numeric": 3, state: 1}


def test_normal_sync_discovers_and_prioritizes_the_missing_quarterly_material(tmp_path):
    index = fixtures._index(tmp_path)
    _structured(index)
    index.publish(("600001",), NOW)
    document = ReportDocument()

    class Source(fixtures.Sources):
        reports = 0

        def financial_reports(self, request, *, observed_at):
            assert request == CompanyFinancialReportRequest("600001", date(2026, 9, 30), AT)
            self.reports += 1
            return (document.disclosure,)

        def fetch(self, query, **kwargs):
            if isinstance(query, DisclosurePartition):
                present = query.market == "sse" and query.start <= AT.date() <= query.end
                return DisclosurePage((document.disclosure,) if present else (), int(present), False)
            return super().fetch(query, **kwargs)

        def document(self, item, *, route="standard"):
            assert item == document.disclosure
            return fixtures._extraction(BODY)

    source = Source()
    result = fixtures._sync_company_evidence(CompanyEvidenceDependencies(index, source, source, lambda: 0), ("600001",), NOW)
    assert not result.failures and source.reports == 1
    assert _latest(index) == replace(_RAW, basic_eps=0.0, roe_pct=0.0, core_profit_growth_pct=34.0, revenue_growth_pct=21.0)


@pytest.mark.parametrize("wrong_expected_value", (False, True))
def test_live_adoption_audit_checks_reviewed_values_original_pdf_receipt_and_production_reader(tmp_path, wrong_expected_value):
    index, document = _setup(tmp_path)
    index.publish(("600001",), NOW)
    annotations = tmp_path / "reviewed.json"
    annotations.write_text(json.dumps({"rows": [{
        "code": "600001", "report_date": "2026-09-30", "disclosed_at": AT.isoformat(), "field": "eps",
        "state": "numeric", "reported_value": 1.0 if wrong_expected_value else 0.0,
        "source_key": document.disclosure.identity, "record_hash": document.disclosure.digest,
        "pdf_sha256": fixtures._extraction(BODY).payload_hash.hex(),
    }]}))
    before = index.path.read_bytes(), index.facts_path.read_bytes()
    audit = AuditCompanyFinancialAdoption(index.path, index.facts_path, annotations, observed_at=NOW,
        event_policy=fixtures.CompanyEventPolicy(), sqlite_policy=SqlitePolicyFixture(), budget_seconds=30)
    assert audit["matched"] == (0 if wrong_expected_value else 1)
    assert audit["status"] == ("degraded" if wrong_expected_value else "passed")
    assert audit["consumer_financial_equal"] and audit["consumer_publication_equal"] and audit["stable_read"]
    assert audit["http_checked"] is False and before == (index.path.read_bytes(), index.facts_path.read_bytes())
