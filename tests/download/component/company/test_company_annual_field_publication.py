"""Independent annual proof commits with facts and cannot become a contract case link."""

from dataclasses import replace
from datetime import datetime, timedelta
from threading import Event

import pytest

from tests.helpers.download.company import company_evidence as fixtures
from tests.helpers.download.company.company_field_recovery import _facts, _put
from tests.helpers.download.company_resources import SQLiteCompanyEvidenceReaderFixture
from trader.download.infra.company_facts_index import ReadCompanyPublicationIdentity
from trader.download.domain.company_financial_report import CompanyFinancialReportRequest
from trader.download.domain.company_evidence import DisclosurePage, DisclosurePartition
from tests.helpers.download.company.company_annual_income import _contract, _PERIOD, _REPORTED
from tests.helpers.download.company.company_annual_income import _Sources, _OPERATING_REVENUE
from trader.download.application.company_evidence import CompanyEvidenceDependencies
from trader.infra.http_contracts import HttpTimeout
from scripts.download.company.company_query_audit import AuditCompanyDatabase
from tests.helpers.shared.configuration import SqlitePolicyFixture
from trader.infra.sqlite_connection import SqliteSession


_ANNUAL_AT = datetime(2026, 4, 10, tzinfo=fixtures.NOW.tzinfo)
_ANNUAL = (
    "证券代码：600001\n本公司2025年年度报告摘要\n近三年主要会计数据和财务指标\n单位：元\n"
    "2025年 2024年 本年比上年增减 2023年\n"
    "营业收入 232,728,984.80 221,337,803.42 5.15% 190,825,407.27\n"
)
_CONTRACT = "本公司已签署正式合同，合同金额为人民币334万元。本次合同签约价占2025年度经审计营业收入的1.35%。"


def _setup(tmp_path, *, disclosed_at=None):
    index = fixtures._index(tmp_path)
    item = fixtures._disclosure("contract", "签订经营合同公告")
    _put(index, item, _CONTRACT)
    index.publish(item.codes, fixtures.NOW)
    facts = _facts(index)
    event, = facts.positive_events
    event = replace(event, measurements=replace(event.measurements, reference_annual_revenue_cny=232728984.8,
                                               reference_disclosed_at=disclosed_at))
    facts = replace(facts, positive_events=(event,))
    with SqliteSession(index.facts_path) as formal:
        parent = ReadCompanyPublicationIdentity(formal)
    index._facts.publish((facts,), {item.identity: item.source}, parent, fixtures.NOW)
    return index, item


def _annual(index, *, key="annual", amount="232,728,984.80", when=_ANNUAL_AT, title="2025年年度报告摘要"):
    annual = fixtures._disclosure(key, title, digest=key, when=when)
    _put(index, annual, _ANNUAL.replace("232,728,984.80", amount))
    return annual


def _field(index, field="annual_revenue"):
    return next(item for item in SQLiteCompanyEvidenceReaderFixture(index.facts_path).read_fields(("600001",)).results
                if item.field == field)


@pytest.mark.parametrize("disclosed_at", (None, _ANNUAL_AT, _ANNUAL_AT + timedelta(days=1)))
def test_catalog_disclosure_proof_binds_the_existing_value_to_exact_official_metadata(tmp_path, monkeypatch, disclosed_at):
    index, contract = _setup(tmp_path, disclosed_at=disclosed_at)
    annual = _annual(index)
    before, = _facts(index).positive_events

    index.prepare_publication(contract.codes, fixtures.NOW)

    assert _facts(index).positive_events == (before,) and before.evidence_ids == (contract.identity,)
    field = _field(index, "revenue_disclosed_at")
    assert field.value == disclosed_at
    if disclosed_at == _ANNUAL_AT:
        assert field.state == "verified" and field.reason == "official_annual_catalog_disclosure"
        proof, = field.evidence
        assert proof.source_key == annual.identity and proof.record_hash == annual.digest
        assert proof.published_at == field.value and proof.rule_id == "official_annual_catalog_disclosure"
        assert proof.quote in _ANNUAL and "2025年" in proof.quote and proof.unit == "time"
    else:
        assert field.state != "verified" and field.evidence == ()

    restarted = fixtures._index(tmp_path)

    def unexpected_read(*args, **kwargs):
        raise AssertionError("the same catalog conclusion must not read annual text again")

    monkeypatch.setattr(restarted._documents, "read", unexpected_read)
    restarted.prepare_publication(contract.codes, fixtures.NOW)
    assert _field(restarted, "revenue_disclosed_at") == field


@pytest.mark.parametrize("conflict", (False, True))
def test_annual_revenue_has_exact_external_source_proof_or_explicitly_stays_unverified(tmp_path, monkeypatch, conflict):
    index, contract = _setup(tmp_path)
    annual = _annual(index)
    if conflict:
        _annual(index, key="conflicting-annual", amount="233,728,984.80")
    before, = _facts(index).positive_events

    index.prepare_publication(contract.codes, fixtures.NOW)

    event, = _facts(index).positive_events
    assert event == before and event.evidence_ids == (contract.identity,)
    field = _field(index)
    assert field.value == 232728984.8
    if conflict:
        assert field.state == "present_unverified" and field.evidence == ()
    else:
        assert field.state == "verified" and field.evidence[0].source_key == annual.identity
        assert field.evidence[0].quote in _ANNUAL and field.evidence[0].published_at == _ANNUAL_AT
        with SqliteSession(index.path) as collected:
            assert collected.run(
                "SELECT 1 FROM company_material_dependencies m JOIN disclosures d ON d.id=m.disclosure_id "
                "WHERE d.source_key=? AND m.reason_code='field_source_proof_required'", (annual.identity,),
            ).fetchone() is not None
    with SqliteSession(index.facts_path) as formal:
        publication = ReadCompanyPublicationIdentity(formal)

    def unexpected_read(*args, **kwargs):
        raise AssertionError("a committed annual source result must be reused across restart")

    restarted = fixtures._index(tmp_path)
    monkeypatch.setattr(restarted._documents, "read", unexpected_read)
    restarted.prepare_publication(contract.codes, fixtures.NOW)
    assert _field(restarted) == field
    with SqliteSession(index.facts_path) as formal:
        assert ReadCompanyPublicationIdentity(formal) == publication


@pytest.mark.parametrize("revision", (False, True))
def test_new_annual_material_invalidates_an_earlier_proof_without_choosing_a_convenient_version(tmp_path, revision):
    index, contract = _setup(tmp_path)
    _annual(index)
    index.prepare_publication(contract.codes, fixtures.NOW)
    assert _field(index).state == "verified"
    _annual(index, key="new-material", amount="233,728,984.80" if revision else "232,728,984.80",
            when=_ANNUAL_AT.replace(day=11), title="2025年年度报告修订版" if revision else "2025年年度报告")
    index.prepare_publication(contract.codes, fixtures.NOW)
    field = _field(index)
    assert field.value == 232728984.8
    assert field.state == ("present_unverified" if revision else "verified")
    if not revision:
        assert field.evidence[0].source_key == "new-material"


def test_annual_source_discovery_queues_only_explicit_dependency_and_survives_directory_refresh(tmp_path):
    index = fixtures._index(tmp_path)
    contract = _contract(index)
    request, = index.report_input_plan(contract.codes, fixtures.NOW).financial_reports
    assert request == CompanyFinancialReportRequest("600001", _PERIOD, _REPORTED)
    annual = fixtures._disclosure("annual-directory", "2025年年度报告摘要", when=_REPORTED)
    with SqliteSession(index.path) as connection:
        partitions = tuple(tuple(row) for row in connection.run("SELECT * FROM partitions"))
    assert index.commit_financial_reports(request, (annual,), fixtures.NOW) > 0
    assert index.report_input_plan(contract.codes, fixtures.NOW).financial_reports == ()
    recent = fixtures._disclosure("newer-unrelated", "关于收到监管警示函的公告")
    index.commit_page(DisclosurePartition("sse", fixtures.NOW.date(), fixtures.NOW.date()),
                      DisclosurePage((recent,), 1, False), fixtures.NOW)
    queued = index.pending_documents(contract.codes, fixtures.NOW, limit=1, policies=fixtures._TEST_DOCUMENT_POLICIES)
    assert tuple(task.disclosure.identity for task in queued) == (annual.identity,)
    audited = AuditCompanyDatabase(index.path, now=fixtures.NOW, budget_seconds=10, sqlite_policy=SqlitePolicyFixture())
    assert audited["summary"]["database"]["pending_documents"] == 2
    with SqliteSession(index.path) as connection:
        assert tuple(tuple(row) for row in connection.run("SELECT * FROM partitions WHERE end_date<?", (fixtures.NOW.date().isoformat(),))) == partitions
    partition = DisclosurePartition("sse", _REPORTED.date(), _REPORTED.date())
    index.commit_page(partition, DisclosurePage((annual,), 1, False), fixtures.NOW)
    restarted = fixtures._index(tmp_path)
    with SqliteSession(restarted.path) as connection:
        assert connection.run(
            "SELECT 1 FROM company_document_requirements r JOIN disclosures d ON d.id=r.disclosure_id WHERE d.source_key=?",
            (annual.identity,),
        ).fetchone() is not None


@pytest.mark.parametrize("kind", ("year", "no_known_report", "wrong_code", "wrong_day", "future", "source"))
def test_annual_source_discovery_cannot_infer_year_or_accept_unbound_metadata(tmp_path, kind):
    index = fixtures._index(tmp_path)
    contract = _contract(index, reference="公司各年度营业收入" if kind == "year" else "公司2025年度经审计营业收入",
                         known_annual=kind != "no_known_report")
    requests = index.report_input_plan(contract.codes, fixtures.NOW).financial_reports
    if kind in {"year", "no_known_report"}:
        assert requests == ()
        return
    annual = fixtures._disclosure("annual-directory", "2025年年度报告摘要", when=_REPORTED)
    if kind == "wrong_code":
        annual = replace(annual, codes=("600002",))
    elif kind == "wrong_day":
        annual = replace(annual, published_at=_REPORTED.replace(day=19))
    elif kind == "future":
        annual = replace(annual, published_at=fixtures.NOW.replace(year=2027))
    else:
        annual = replace(annual, source="news")
    with pytest.raises(ValueError, match="company_annual_report_input_invalid"):
        index.commit_financial_reports(requests[0], (annual,), fixtures.NOW)
    assert index.report_input_plan(contract.codes, fixtures.NOW).financial_reports == requests


@pytest.mark.parametrize("outcome", ("verified", "timeout", "cancelled"))
def test_normal_sync_discovers_and_processes_annual_source_or_preserves_degraded_publication(tmp_path, outcome):
    index = fixtures._index(tmp_path)
    contract = _contract(index)
    annual = fixtures._disclosure("official-annual", "2025年年度报告摘要", when=_REPORTED)
    cancelled = Event()

    class Source(_Sources):
        report_calls = 0

        def fetch(self, query, **kwargs):
            page = super().fetch(query, **kwargs)
            if (outcome == "verified" and isinstance(query, DisclosurePartition) and query.market == "sse"
                and query.start <= annual.published_at.date() <= query.end):
                return replace(page, rows=(*page.rows, annual), total=page.total + 1)
            return page

        def financial_reports(self, request, *, observed_at):
            self.report_calls += 1
            if outcome == "timeout":
                raise HttpTimeout("fixture_timeout")
            if outcome == "cancelled":
                cancelled.set()
            return (annual,)

        def document(self, item, *, route="standard"):
            assert item == annual
            return fixtures._extraction(_ANNUAL.replace("232,728,984.80", f"{_OPERATING_REVENUE:,.2f}"))

    source = Source(contract)
    result = fixtures._sync_company_evidence(
        CompanyEvidenceDependencies(index, source, source, lambda: 0, cancel_requested=cancelled.is_set),
        contract.codes, fixtures.NOW,
    )
    assert source.report_calls == 1 and result.published_codes == 1
    if outcome == "cancelled":
        assert result.failures == ("task:cancelled",)
        with SqliteSession(index.path) as connection:
            assert connection.run("SELECT 1 FROM disclosures WHERE source_key=?", (annual.identity,)).fetchone() is None
    elif outcome == "timeout":
        assert "annual_income:timeout" in result.failures and _field(index).state == "present_unverified"
    else:
        field = _field(index)
        assert field.state == "verified" and field.value == _OPERATING_REVENUE
        assert field.evidence[0].source_key == annual.identity
        restarted = fixtures._index(tmp_path)
        fixtures._sync_company_evidence(
            CompanyEvidenceDependencies(restarted, source, source, lambda: 0), contract.codes, fixtures.NOW + timedelta(minutes=1),
        )
        assert source.report_calls == 1 and _field(restarted) == field
