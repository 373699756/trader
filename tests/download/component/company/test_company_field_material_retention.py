"""Unproved published fields pin existing material without reopening downloads."""

import hashlib
from dataclasses import replace
from datetime import timedelta

import pytest

from tests.helpers.download.company import company_evidence as fixtures
from tests.helpers.download.company_resources import CompanyDocumentFilesFixture, SQLiteCompanyEvidenceIndexFixture, SQLiteCompanyEvidenceReaderFixture, SQLiteCompanyFactsIndexFixture
from tests.helpers.download.configuration import CompanyEventPolicyFixture
from trader.download.domain.company_event_extraction import ExtractCompanyEvents, ExtractCompanyFieldEvidence
from trader.download.domain.company_evidence import CompanyDocument, DisclosurePage, DisclosurePartition
from trader.download.infra.company_fact_rows import ReadCompanyFactRows
from trader.download.infra.company_facts_index import CompanyInputReceipt, ReadCompanyPublicationIdentity
from trader.download.infra.company_field_recovery import (
    ReadRequiredCompanyMaterialDependencies, RegisterCompanyMaterialDependencies,
)
from trader.download.infra.company_field_result_rows import ReadCompanyFieldResults
from trader.download.infra.company_raw_files import CompanyRawFiles
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction


_AT = fixtures.NOW
_BODY = (
    "证券代码：600001\n本公司首次回购股份100万股，占公司总股本的比例为0.3%。\n"
    "回购股份用于注销。\n公司董事会保证本次披露的信息真实、准确、完整，相关安排请以正式披露的本次回购实施情况为准。"
)


def _index(root):
    raw = CompanyRawFiles(root, capacity=10_000_000, analysis_reserve=1_000_000, free_reserve=1000)
    index = SQLiteCompanyEvidenceIndexFixture(
        root / "company_evidence.sqlite3", facts_path=root / "company_facts.sqlite3",
        documents=CompanyDocumentFilesFixture(root / "text"), raw_files=raw,
    )
    index.initialize()
    return index, raw


def _legacy(root):
    index, raw = _index(root)
    index.publish(("600001",), _AT)
    base = SQLiteCompanyEvidenceReaderFixture(index.facts_path).read(("600001",))[0]
    item = fixtures._disclosure("field-material", "关于首次回购公司股份的公告", digest="old-record")
    partition = DisclosurePartition("sse", item.published_at.date(), item.published_at.date())
    index.commit_page(partition, DisclosurePage((item,), 1, False), _AT)
    directory = raw.directory(item)
    directory.mkdir(parents=True)
    partial = directory / "download.part"
    partial.write_bytes(b"%PDF-existing-legacy-field-material")
    local = raw.complete(item, partial)
    index.document_stage(item, "downloaded")
    assert fixtures._extraction(_BODY).text == _BODY
    assert index.commit_document(item, fixtures._extraction(_BODY), _AT)
    document = CompanyDocument(item, _BODY, True)
    extracted = ExtractCompanyEvents((document,), _AT, CompanyEventPolicyFixture())
    assert len(extracted.positives) == 1
    fact = replace(base, positive_events=extracted.positives, review_reasons=())
    writer = SQLiteCompanyFactsIndexFixture(index.facts_path)
    with SqliteSession(index.facts_path) as formal:
        parent = ReadCompanyPublicationIdentity(formal)
    body_hash = hashlib.sha256(_BODY.encode()).digest()
    writer.publish((fact,), {item.identity: item.source}, parent, _AT,
                   receipts=(CompanyInputReceipt(item.source, item.identity, "600001", item.digest,
                                                 writer.extraction_hash, body_hash, _AT),))
    with SqliteSession(index.path) as collected, SqliteTransaction(collected):
        identifier = collected.run("SELECT id FROM disclosures WHERE source_key=?", (item.identity,)).require_one()[0]
        collected.run("INSERT INTO document_publications VALUES(?,?,?,?,?)",
                      (identifier, "600001", item.digest, writer.extraction_hash, _AT.isoformat()))
        collected.run_many("INSERT INTO company_field_repairs VALUES(?,?,?,?,?,?,'complete')", (
            (identifier, "600001", item.digest, fact.positive_events[0].event_id, field, "old-field-rule")
            for field in ("actual_pct", "repurchase_purpose")
        ))
    return index, raw, document, local


def _register(index, *, extraction_hash=None):
    with SqliteSession(index.path) as collected, SqliteSession(index.facts_path) as formal, SqliteTransaction(collected):
        RegisterCompanyMaterialDependencies(
            collected, formal, ReadCompanyFactRows(formal, ("600001",)),
            extraction_hash if extraction_hash is not None else index._facts.extraction_hash,
        )


def _dependencies(index):
    with SqliteSession(index.path) as collected:
        return ReadRequiredCompanyMaterialDependencies(collected, index._facts.extraction_hash)


def _verify(index, document, *, only=None, event_policy=None, receipts=()):
    writer = SQLiteCompanyFactsIndexFixture(index.facts_path, event_policy=event_policy or CompanyEventPolicyFixture())
    proofs = ExtractCompanyFieldEvidence(document, _AT, CompanyEventPolicyFixture())
    assert {item.field for item in proofs} == {"actual_pct", "repurchase_purpose"}
    with SqliteSession(index.facts_path) as formal:
        parent = ReadCompanyPublicationIdentity(formal)
        previous = ReadCompanyFieldResults(formal, ("600001",))
    fields = tuple(next((proof for proof in proofs if proof.field == item.field and (only is None or proof.field == only)), item)
                   for item in previous)
    writer.publish((), {}, parent, _AT + timedelta(minutes=1), field_results=fields, receipts=receipts)
    return writer.extraction_hash


def test_present_unverified_values_keep_text_and_pdf_despite_old_complete_repairs(tmp_path):
    index, _raw, document, local = _legacy(tmp_path)
    _register(index)
    index.finish(_AT)
    dependencies = _dependencies(index)
    assert len(dependencies) == 1 and dependencies[0].reason_code == "field_source_proof_required"
    assert dependencies[0].record_hash == document.disclosure.digest and dependencies[0].is_current_version
    assert local.path.is_file()
    assert index._documents.read(dependencies[0].body_hash) == _BODY
    with SqliteSession(index.path) as collected:
        assert {row[0] for row in collected.run("SELECT state FROM company_field_repairs")} == {"complete"}
    assert index.pending_documents(document.disclosure.codes, _AT, policies=fixtures._TEST_DOCUMENT_POLICIES) == ()


def test_complete_field_proofs_release_only_the_corresponding_current_material(tmp_path):
    index, _raw, document, local = _legacy(tmp_path)
    _register(index)
    _verify(index, document)
    _register(index)
    index.finish(_AT)
    assert _dependencies(index) == ()
    assert not local.path.exists() and not tuple((tmp_path / "text").rglob("*.gz"))


def test_one_verified_field_does_not_release_other_unverified_field_material(tmp_path):
    index, _raw, document, local = _legacy(tmp_path)
    _register(index)
    _verify(index, document, only="actual_pct")
    _register(index)
    index.finish(_AT)
    assert _dependencies(index) and local.path.is_file()
    assert index._documents.read(_dependencies(index)[0].body_hash) == _BODY


def test_startup_restores_field_protection_before_discarding_an_older_record(tmp_path):
    index, _raw, document, local = _legacy(tmp_path)
    _register(index)
    original = _dependencies(index)[0]
    revised = replace(document.disclosure, digest="revised-record")
    partition = DisclosurePartition("sse", revised.published_at.date(), revised.published_at.date())
    index.commit_page(partition, DisclosurePage((revised,), 1, False), _AT)
    with SqliteSession(index.path) as collected, SqliteTransaction(collected):
        collected.run("DELETE FROM company_material_dependencies")
    restarted, _raw = _index(tmp_path)
    required = _dependencies(restarted)
    assert len(required) == 1 and required[0].record_hash == original.record_hash
    assert required[0].body_hash == original.body_hash and required[0].extraction_hash == original.extraction_hash
    assert required[0].current_record_hash == revised.digest and not required[0].is_current_version
    assert local.path.is_file() and restarted._documents.read(original.body_hash) == _BODY


def test_unread_input_does_not_release_existing_field_dependency(tmp_path):
    index, _raw, document, local = _legacy(tmp_path)
    _register(index)
    _verify(index, document)
    with SqliteSession(index.path) as collected, SqliteTransaction(collected):
        collected.run("UPDATE disclosures SET body_checked=0")
    _register(index)
    index.finish(_AT)
    assert _dependencies(index) and local.path.is_file()


def test_changed_extraction_identity_does_not_release_an_original_field_dependency(tmp_path):
    index, _raw, document, local = _legacy(tmp_path)
    _register(index)
    original = _dependencies(index)[0]
    policy = replace(CompanyEventPolicyFixture(), risk_days=367)
    writer = SQLiteCompanyFactsIndexFixture(index.facts_path, event_policy=policy)
    receipt = CompanyInputReceipt(
        document.disclosure.source, document.disclosure.identity, "600001", document.disclosure.digest,
        writer.extraction_hash, original.body_hash, _AT + timedelta(minutes=1),
    )
    _verify(index, document, event_policy=policy, receipts=(receipt,))
    _register(index, extraction_hash=writer.extraction_hash)
    assert _dependencies(index)[0].extraction_hash == original.extraction_hash
    assert local.path.is_file()


@pytest.mark.parametrize("column,value", (("code", "600002"), ("record_hash", "other-record"), ("body_hash", b"x" * 32)))
def test_other_security_or_version_receipt_cannot_create_a_field_material_dependency(tmp_path, column, value):
    index, _raw, _document, _local = _legacy(tmp_path)
    with SqliteSession(index.facts_path) as formal, SqliteTransaction(formal):
        formal.run(f"UPDATE company_input_receipt SET {column}=? WHERE outcome='valid'", (value,))
    _register(index)
    assert _dependencies(index) == ()


def test_missing_formal_publication_does_not_pin_unpublished_field_claims(tmp_path):
    index, _raw, _document, _local = _legacy(tmp_path)
    with SqliteSession(index.facts_path) as formal, SqliteTransaction(formal):
        formal.run("DELETE FROM company_publication")
    _register(index)
    assert _dependencies(index) == ()


def test_verified_current_revision_does_not_discharge_the_old_record_dependency(tmp_path):
    index, _raw, document, local = _legacy(tmp_path)
    _register(index)
    original = _dependencies(index)[0]
    revised = replace(document.disclosure, digest="current-revision")
    partition = DisclosurePartition("sse", revised.published_at.date(), revised.published_at.date())
    index.commit_page(partition, DisclosurePage((revised,), 1, False), _AT)
    assert index.commit_document(revised, fixtures._extraction(_BODY), _AT)
    current = CompanyDocument(revised, _BODY, True)
    receipt = CompanyInputReceipt(
        revised.source, revised.identity, "600001", revised.digest,
        index._facts.extraction_hash, original.body_hash, _AT + timedelta(minutes=1),
    )
    _verify(index, current, receipts=(receipt,))
    _register(index)
    assert any(item.record_hash == original.record_hash for item in _dependencies(index))
    index.finish(_AT)
    assert local.path.is_file() and index._documents.read(original.body_hash) == _BODY


def test_missing_local_material_is_reported_as_missing_without_reopening_a_download(tmp_path):
    index, raw, document, _local = _legacy(tmp_path)
    body_hash = hashlib.sha256(_BODY.encode()).digest()
    with SqliteSession(index.path) as collected, SqliteTransaction(collected):
        collected.run("UPDATE disclosures SET body_retained=0")
    index._documents.release(frozenset())
    assert raw.release(document.disclosure)
    restarted, _raw = _index(tmp_path)
    required = _dependencies(restarted)
    assert len(required) == 1 and required[0].body_hash == body_hash
    assert not restarted._documents._path(body_hash).exists()
    assert restarted.pending_documents(document.disclosure.codes, _AT, policies=fixtures._TEST_DOCUMENT_POLICIES) == ()
    with SqliteSession(restarted.facts_path) as formal:
        assert {item.state for item in ReadCompanyFieldResults(formal, ("600001",))} == {"present_unverified"}


def test_no_event_fields_do_not_create_material_obligations(tmp_path):
    index, _raw = _index(tmp_path)
    index.publish(("600001",), _AT)
    _register(index)
    assert _dependencies(index) == ()
