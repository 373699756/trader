"""Independent annual proof commits with facts and cannot become a contract case link."""

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from tests.helpers.download.company import company_evidence as fixtures
from tests.helpers.download.company.company_field_recovery import _facts, _put
from tests.helpers.download.company_resources import CompanyDocumentFilesFixture, SQLiteCompanyEvidenceReaderFixture
from trader.download.infra.company_facts_index import ReadCompanyPublicationIdentity
from trader.download.infra.company_fact_rows import ReadCompanyFactRows
from trader.download.infra.company_annual_field_publication import CanReuseCompanyAnnualFieldResults
from trader.download.infra.company_field_publication import PrepareCompanyFieldResults
from trader.infra.sqlite_connection import SqliteSession
from trader.download.infra import company_annual_field_publication


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


def test_explicit_annual_revalidation_does_not_reuse_cached_proofs_or_attempts(tmp_path, monkeypatch):
    index, contract = _setup(tmp_path, disclosed_at=_ANNUAL_AT)
    annual = _annual(index)
    index.prepare_and_publish(contract.codes, fixtures.NOW)
    assert _field(index).state == _field(index, "revenue_disclosed_at").state == "verified"
    documents = CompanyDocumentFilesFixture(index.path.parent / "text")
    documents.write(_ANNUAL)
    with SqliteSession(index.path) as collected:
        collected.run("UPDATE disclosures SET body_retained=1 WHERE source_key=?", (annual.identity,))
    facts = _facts(index)
    before = index.path.read_bytes(), index.facts_path.read_bytes()
    parsed = []

    def no_longer_supported(document):
        parsed.append(document.disclosure.identity)
        return None

    monkeypatch.setattr(company_annual_field_publication, "ExtractCompanyAnnualReportRevenue", no_longer_supported)
    with SqliteSession(index.path) as collected, SqliteSession(index.facts_path) as formal:
        result = PrepareCompanyFieldResults(
            collected, formal, facts, (), observed_at=fixtures.NOW,
            extraction_hash=ReadCompanyPublicationIdentity(formal).extraction_hash,
            event_policy=fixtures.CompanyEventPolicy(), read_document=documents.read, revalidate=True,
        )
    assert parsed and set(parsed) == {annual.identity}
    assert all(item.state == "present_unverified" and not item.evidence for item in result.results
               if item.field in {"annual_revenue", "revenue_disclosed_at"})
    assert before == (index.path.read_bytes(), index.facts_path.read_bytes())
    assert _field(index).state == _field(index, "revenue_disclosed_at").state == "verified"


@pytest.mark.parametrize("disclosed_at", (None, _ANNUAL_AT, _ANNUAL_AT + timedelta(days=1)))
def test_catalog_disclosure_proof_binds_the_existing_value_to_exact_official_metadata(tmp_path, monkeypatch, disclosed_at):
    index, contract = _setup(tmp_path, disclosed_at=disclosed_at)
    annual = _annual(index)
    before, = _facts(index).positive_events

    index.prepare_and_publish(contract.codes, fixtures.NOW)

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
    restarted.prepare_and_publish(contract.codes, fixtures.NOW)
    assert _field(restarted, "revenue_disclosed_at") == field


@pytest.mark.parametrize("conflict", (False, True))
def test_annual_revenue_has_exact_external_source_proof_or_explicitly_stays_unverified(tmp_path, monkeypatch, conflict):
    index, contract = _setup(tmp_path)
    annual = _annual(index)
    if conflict:
        _annual(index, key="conflicting-annual", amount="233,728,984.80")
    before, = _facts(index).positive_events

    index.prepare_and_publish(contract.codes, fixtures.NOW)

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
    restarted.prepare_and_publish(contract.codes, fixtures.NOW)
    assert _field(restarted) == field
    with SqliteSession(index.facts_path) as formal:
        assert ReadCompanyPublicationIdentity(formal) == publication


def test_annual_attempt_receipt_cannot_replace_missing_current_valid_receipt(tmp_path):
    index, contract = _setup(tmp_path, disclosed_at=_ANNUAL_AT)
    annual = _annual(index)
    index.prepare_and_publish(contract.codes, fixtures.NOW)
    facts = _facts(index)
    fields = tuple(_field(index, field) for field in ("annual_revenue", "revenue_disclosed_at"))
    assert all(field.state == "verified" for field in fields)
    with SqliteSession(index.facts_path) as formal:
        formal.run("DELETE FROM company_input_receipt WHERE source_key=? AND outcome='valid'", (annual.identity,))

    def unexpected_read(_digest):
        raise AssertionError("missing valid receipt must not authorize a body read")

    with SqliteSession(index.path) as collected, SqliteSession(index.facts_path) as formal:
        publication = ReadCompanyPublicationIdentity(formal)
        assert all(CanReuseCompanyAnnualFieldResults(collected, formal, facts, facts.positive_events[0], field)
                   for field in fields)
        before = tuple(tuple(row) for row in formal.run("SELECT * FROM company_input_receipt ORDER BY source_key,extraction_hash"))
        prepared = PrepareCompanyFieldResults(
            collected, formal, facts, (), observed_at=fixtures.NOW,
            extraction_hash=publication.extraction_hash, event_policy=fixtures.CompanyEventPolicy(),
            read_document=unexpected_read,
        )
        after = tuple(tuple(row) for row in formal.run("SELECT * FROM company_input_receipt ORDER BY source_key,extraction_hash"))
        actual = tuple(field for field in prepared.results if field.field in {"annual_revenue", "revenue_disclosed_at"})
        assert len(actual) == 2 and all(field.state == "present_unverified" and not field.evidence for field in actual)
        assert {field.field: field.value for field in actual} == {field.field: field.value for field in fields}
        assert not prepared.receipts and before == after
        assert ReadCompanyFactRows(formal, contract.codes) == (facts,)
        assert ReadCompanyPublicationIdentity(formal) == publication


@pytest.mark.parametrize("revision", (False, True))
def test_new_annual_material_invalidates_an_earlier_proof_without_choosing_a_convenient_version(tmp_path, revision):
    index, contract = _setup(tmp_path)
    _annual(index)
    index.prepare_and_publish(contract.codes, fixtures.NOW)
    assert _field(index).state == "verified"
    _annual(index, key="new-material", amount="233,728,984.80" if revision else "232,728,984.80",
            when=_ANNUAL_AT.replace(day=11), title="2025年年度报告修订版" if revision else "2025年年度报告")
    index.prepare_and_publish(contract.codes, fixtures.NOW)
    field = _field(index)
    assert field.value == 232728984.8
    assert field.state == ("present_unverified" if revision else "verified")
    if not revision:
        assert field.evidence[0].source_key == "new-material"
