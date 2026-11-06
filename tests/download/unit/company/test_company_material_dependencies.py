"""Unresolved review inputs have versioned dependencies, never fake field completion."""

from dataclasses import replace
from datetime import timedelta

import pytest

from tests.helpers.download.company import company_evidence as fixtures
from tests.helpers.download.company import company_field_recovery as field_fixtures
from tests.helpers.shared.configuration import (
    CompanyEventPolicyFixture,
    CompanyDocumentFilesFixture,
    SQLiteCompanyEvidenceIndexFixture,
    SQLiteCompanyEvidenceReaderFixture,
    SQLiteCompanyFactsIndexFixture,
)
from trader.download.domain.company_events import CompanyReviewReason
from trader.download.domain.company_evidence import DisclosurePage, DisclosurePartition
from trader.download.infra.company_facts_index import ReadCompanyPublicationIdentity
from trader.download.infra.company_field_recovery import (
    ReadCompanyFieldRecovery,
    ReadRequiredCompanyMaterialDependencies,
    RegisterCompanyMaterialDependencies,
)
from trader.download.infra.company_raw_files import CompanyRawFiles
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction

NOW = fixtures.NOW
_DETAIL = "相关事项仍在核实，具体签署主体及后续安排请以正式文件为准，相关资料并未作出进一步说明。"


def _index(root, *, batch=False, event_policy=None):
    raw = CompanyRawFiles(root, capacity=10_000_000, analysis_reserve=1_000_000, free_reserve=1000)
    index = SQLiteCompanyEvidenceIndexFixture(
        root / "company_evidence.sqlite3",
        facts_path=root / "company_facts.sqlite3",
        documents=CompanyDocumentFilesFixture(root / "text"),
        raw_files=raw,
        batch_publication_reads=batch,
        event_policy=event_policy or CompanyEventPolicyFixture(),
    )
    index.initialize()
    return index, raw


def _stage(index, raw, item, body):
    partition = DisclosurePartition("sse", item.published_at.date(), item.published_at.date())
    index.commit_page(partition, DisclosurePage((item,), 1, False), NOW)
    directory = raw.directory(item)
    directory.mkdir(parents=True, exist_ok=True)
    partial = directory / "download.part"
    partial.write_bytes(b"%PDF-isolated-material-dependency")
    local = raw.complete(item, partial)
    index.document_stage(item, "downloaded")
    assert index.commit_document(item, fixtures._extraction(body), NOW)
    return local


def _facts(index):
    return SQLiteCompanyEvidenceReaderFixture(index.facts_path).read(("600001",))[0]


def _dependencies(index):
    with SqliteSession(index.path) as connection:
        return ReadRequiredCompanyMaterialDependencies(connection, index._facts.extraction_hash)


def _publish_formal_fact(index, fact):
    formal_index = SQLiteCompanyFactsIndexFixture(index.facts_path)
    with SqliteSession(index.facts_path) as formal:
        parent = ReadCompanyPublicationIdentity(formal)
    formal_index.publish((fact,), {"reviewed": "cninfo"}, parent, NOW + timedelta(minutes=1))
    with SqliteSession(index.path) as collected, SqliteSession(index.facts_path) as formal, SqliteTransaction(collected):
        RegisterCompanyMaterialDependencies(collected, formal, (fact,), formal_index.extraction_hash)


def test_review_only_material_survives_publish_resume_and_capacity_backpressure(tmp_path):
    index, raw = _index(tmp_path)
    item = fixtures._disclosure("reviewed", "关于签订重大合同的公告")
    local = _stage(index, raw, item, _DETAIL)
    index.publish(item.codes, NOW)
    facts = _facts(index)
    assert facts.positive_events == ()
    assert any(review.reason_code == "subject_unverified" for review in facts.review_reasons)
    assert len(_dependencies(index)) == 1
    assert local.path.is_file()
    assert tuple((tmp_path / "text").rglob("*.gz"))
    with SqliteSession(index.path) as collected:
        assert collected.run("SELECT COUNT(*) FROM company_field_repairs").require_one()[0] == 0
    with pytest.raises(RuntimeError, match="company_raw_capacity_exhausted"), raw.reserve(raw.directory(item), raw.capacity):
        pytest.fail("capacity failure must apply backpressure")
    assert local.path.is_file()

    restarted, _raw = _index(tmp_path)
    restarted.prepare_publication(item.codes, NOW + timedelta(minutes=1))
    assert restarted.pending_documents(item.codes, NOW, policies=fixtures._TEST_DOCUMENT_POLICIES) == ()
    assert _dependencies(restarted) == _dependencies(index)
    assert local.path.is_file()


def test_review_dependency_is_reconstructed_after_formal_commit_collection_rollback(tmp_path):
    index, raw = _index(tmp_path)
    item = fixtures._disclosure("reviewed", "关于签订重大合同的公告")
    local = _stage(index, raw, item, _DETAIL)
    index.publish(item.codes, NOW)
    expected = _dependencies(index)
    with SqliteSession(index.path) as collected, SqliteTransaction(collected):
        collected.run("DELETE FROM company_material_dependencies")
    restarted, _raw = _index(tmp_path)
    restarted.prepare_publication(item.codes, NOW + timedelta(minutes=1))
    assert _dependencies(restarted) == expected
    assert local.path.is_file()
    assert restarted.pending_documents(item.codes, NOW, policies=fixtures._TEST_DOCUMENT_POLICIES) == ()


def test_legacy_missing_review_material_is_reported_without_reopening_valid_document(tmp_path):
    index, raw = _index(tmp_path)
    item = fixtures._disclosure("reviewed", "关于签订重大合同的公告")
    _stage(index, raw, item, _DETAIL)
    index.publish(item.codes, NOW)
    with SqliteSession(index.path) as collected, SqliteTransaction(collected):
        collected.run("DELETE FROM company_material_dependencies")
        collected.run("UPDATE disclosures SET body_retained=0")
    index._documents.release(frozenset())
    assert raw.release(item)
    restarted, _raw = _index(tmp_path)
    restarted.prepare_publication(item.codes, NOW + timedelta(minutes=1))
    assert len(_dependencies(restarted)) == 1
    assert restarted.pending_documents(item.codes, NOW, policies=fixtures._TEST_DOCUMENT_POLICIES) == ()
    assert not tuple((tmp_path / "text").rglob("*.gz"))


def test_first_dependency_projection_recovers_original_receipt_after_missing_text_rejection(tmp_path):
    index, raw = _index(tmp_path)
    item = fixtures._disclosure("reviewed", "关于签订重大合同的公告")
    original = _stage(index, raw, item, _DETAIL)
    index.publish(item.codes, NOW)
    original_hash = _dependencies(index)[0].body_hash
    with SqliteSession(index.path) as collected, SqliteTransaction(collected):
        collected.run("DELETE FROM company_material_dependencies")
    index._documents.release(frozenset())
    restarted, _raw = _index(tmp_path)
    restarted.prepare_publication(item.codes, NOW + timedelta(minutes=1))
    required = _dependencies(restarted)[0]
    assert required.body_hash == original_hash and required.current_body_hash is None
    assert not required.is_current_version and original.path.is_file()
    with SqliteSession(index.path) as collected:
        assert tuple(collected.run("SELECT body_retained,body_hash FROM disclosures").require_one()) == (0, None)


@pytest.mark.parametrize("column,value", [
    ("source", 1), ("record_hash", "wrong-version"), ("body_hash", b"x" * 32), ("extraction_hash", b"x" * 32),
])
def test_review_dependency_requires_exact_current_valid_receipt(tmp_path, column, value):
    index, raw = _index(tmp_path)
    item = fixtures._disclosure("reviewed", "关于签订重大合同的公告")
    _stage(index, raw, item, _DETAIL)
    index.publish(item.codes, NOW)
    facts = _facts(index)
    with SqliteSession(index.path) as collected, SqliteSession(index.facts_path) as formal, SqliteTransaction(collected):
        collected.run("DELETE FROM company_material_dependencies")
        with SqliteTransaction(formal):
            formal.run(f"UPDATE company_input_receipt SET {column}=? WHERE outcome='valid'", (value,))
        RegisterCompanyMaterialDependencies(collected, formal, (facts,), index._facts.extraction_hash)
    assert _dependencies(index) == ()


def test_unread_review_does_not_release_dependency_but_processed_resolution_does(tmp_path):
    index, raw = _index(tmp_path)
    item = fixtures._disclosure("reviewed", "关于签订重大合同的公告")
    local = _stage(index, raw, item, _DETAIL)
    index.publish(item.codes, NOW)
    facts = _facts(index)
    pending = replace(facts, review_reasons=(
        CompanyReviewReason("positive", "document_pending", item.identity, NOW, "待处理正文"),
    ))
    _publish_formal_fact(index, pending)
    index.finish(NOW)
    assert _dependencies(index)
    assert local.path.is_file()
    resolved = replace(facts, review_reasons=tuple(review for review in facts.review_reasons if review.evidence_id != item.identity))
    _publish_formal_fact(index, resolved)
    index.finish(NOW)
    assert _dependencies(index) == ()
    assert not local.path.exists()
    assert not tuple((tmp_path / "text").rglob("*.gz"))


def test_publication_batch_candidate_preserves_facts_receipts_and_generation(tmp_path):
    results = []
    for batch in (False, True):
        index, raw = _index(tmp_path / str(batch), batch=batch)
        item = replace(fixtures._disclosure("reviewed", "关于签订重大合同的公告"), codes=("600001", "600002"))
        _stage(index, raw, item, "本公司已签订正式合同，合同已生效。合同金额人民币2亿元（不含税）。" + _DETAIL)
        with SqliteSession(index.path) as collected:
            assert collected.run("SELECT COUNT(*) FROM document_link_candidates").require_one()[0] > 0
        index.publish(item.codes, NOW)
        with SqliteSession(index.facts_path) as formal:
            results.append((
                tuple(tuple(row) for row in formal.run("SELECT * FROM company_input_receipt ORDER BY code")),
                ReadCompanyPublicationIdentity(formal),
                SQLiteCompanyEvidenceReaderFixture(index.facts_path).read(item.codes),
            ))
    assert results[0] == results[1]


def test_new_document_publication_is_urgent_only_for_its_other_current_pending_fields(tmp_path):
    index, old = field_fixtures._legacy_reduction(tmp_path)
    index.prepare_publication(old.codes, NOW)
    later = replace(old, identity="new-progress", digest="new-version")
    assert index.document_publication_urgent(later)
    assert not index.document_publication_urgent(old)
    assert not index.document_publication_urgent(replace(later, codes=("600002",)))
    with SqliteSession(index.path) as collected, SqliteTransaction(collected):
        collected.run("UPDATE company_field_repairs SET state='attempted'")
    assert not index.document_publication_urgent(later)
    with SqliteSession(index.path) as collected, SqliteTransaction(collected):
        collected.run("UPDATE company_field_repairs SET state='pending',record_hash='obsolete-version'")
    assert not index.document_publication_urgent(later)


def test_record_revision_preserves_original_needed_material_until_new_verified_resolution(tmp_path):
    index, raw = _index(tmp_path)
    item = fixtures._disclosure("reviewed", "关于签订重大合同的公告")
    original = _stage(index, raw, item, _DETAIL)
    index.publish(item.codes, NOW)
    dependency = _dependencies(index)[0]
    original_text = index._documents._path(dependency.body_hash)
    revised = replace(item, digest="revised-record")
    partition = DisclosurePartition("sse", revised.published_at.date(), revised.published_at.date())
    index.commit_page(partition, DisclosurePage((revised,), 1, False), NOW)
    index.finish(NOW)
    assert original.path.is_file() and original_text.is_file()
    required = _dependencies(index)[0]
    assert required.record_hash == item.digest and required.current_record_hash == revised.digest
    assert required.current_body_hash is None and not required.is_current_version
    restarted, raw = _index(tmp_path)
    restarted.prepare_publication(item.codes, NOW + timedelta(minutes=1))
    assert original.path.is_file() and original_text.is_file()
    tasks = restarted.pending_documents(item.codes, NOW, policies=fixtures._TEST_DOCUMENT_POLICIES)
    assert [task.disclosure.digest for task in tasks] == [revised.digest]
    current = _stage(restarted, raw, revised, "公司全资子公司甲公司已签订正式合同。合同金额人民币2亿元。" + _DETAIL)
    restarted.publish(item.codes, NOW)
    assert _dependencies(restarted) == ()
    assert not original.path.exists() and not current.path.exists() and not original_text.exists()


def test_first_projection_before_cleanup_preserves_old_record_material_after_directory_revision(tmp_path):
    index, raw = _index(tmp_path)
    item = fixtures._disclosure("reviewed", "关于签订重大合同的公告")
    original = _stage(index, raw, item, _DETAIL)
    index.publish(item.codes, NOW)
    old_body = _dependencies(index)[0].body_hash
    old_text = index._documents._path(old_body)
    revised = replace(item, digest="new-directory-record")
    partition = DisclosurePartition("sse", revised.published_at.date(), revised.published_at.date())
    index.commit_page(partition, DisclosurePage((revised,), 1, False), NOW)
    with SqliteSession(index.path) as collected, SqliteTransaction(collected):
        collected.run("DROP TABLE company_material_dependencies")
    restarted, _raw = _index(tmp_path)
    required = _dependencies(restarted)[0]
    assert required.record_hash == item.digest and required.body_hash == old_body
    assert required.current_record_hash == revised.digest and required.current_body_hash is None
    assert not required.is_current_version and original.path.is_file() and old_text.is_file()
    with SqliteSession(restarted.path) as collected:
        assert tuple(collected.run("SELECT body_checked,body_retained,body_hash FROM disclosures").require_one()) == (0, 0, None)


def test_body_revision_keeps_original_provenance_without_upgrading_its_receipt(tmp_path):
    index, raw = _index(tmp_path)
    item = fixtures._disclosure("reviewed", "关于签订重大合同的公告")
    original = _stage(index, raw, item, _DETAIL)
    index.publish(item.codes, NOW)
    dependency = _dependencies(index)[0]
    original_text = index._documents._path(dependency.body_hash)
    with SqliteSession(index.path) as collected, SqliteTransaction(collected):
        collected.run("UPDATE disclosures SET body_checked=0")
    assert index.commit_document(item, fixtures._extraction(_DETAIL + "本次重新核对正文，仍须核实实际签署主体。"), NOW)
    index.finish(NOW)
    required = _dependencies(index)[0]
    assert required.body_hash == dependency.body_hash
    assert required.current_body_hash != required.body_hash and not required.is_current_version
    assert original.path.is_file() and original_text.is_file()
    with SqliteSession(index.facts_path) as formal:
        assert formal.run("SELECT body_hash FROM company_input_receipt WHERE outcome='valid'").require_one()[0] == dependency.body_hash


def test_new_extraction_identity_does_not_discard_original_review_dependency(tmp_path):
    index, raw = _index(tmp_path)
    item = fixtures._disclosure("reviewed", "关于签订重大合同的公告")
    original = _stage(index, raw, item, _DETAIL)
    index.publish(item.codes, NOW)
    old_hash = index._facts.extraction_hash
    policy = CompanyEventPolicyFixture()
    restarted, _raw = _index(tmp_path, event_policy=replace(policy, risk_days=policy.risk_days + 1))
    restarted.prepare_publication(item.codes, NOW + timedelta(minutes=1))
    required = _dependencies(restarted)
    assert len(required) == 2
    assert {dependency.extraction_hash for dependency in required} == {old_hash, restarted._facts.extraction_hash}
    assert any(not dependency.is_current_version for dependency in required)
    assert original.path.is_file()


def test_reparse_without_candidate_removes_only_current_record_and_extraction_candidate(tmp_path):
    index, raw = _index(tmp_path)
    item = fixtures._disclosure("reviewed", "关于签订重大合同的公告")
    _stage(index, raw, item, "本公司已签订正式合同。合同金额人民币2亿元（不含税）。" + _DETAIL)
    other_hash = b"x" * 32
    with SqliteSession(index.path) as collected, SqliteTransaction(collected):
        assert collected.run("SELECT COUNT(*) FROM document_link_candidates").require_one()[0] == 1
        collected.run(
            "INSERT INTO document_link_candidates SELECT source,source_key,record_hash,?,payload FROM document_link_candidates",
            (other_hash,),
        )
        collected.run("UPDATE disclosures SET body_checked=0")
    assert index.commit_document(item, fixtures._extraction("公司全资子公司甲公司已签订正式合同。合同金额人民币2亿元。" + _DETAIL), NOW)
    with SqliteSession(index.path) as collected:
        assert tuple(row[0] for row in collected.run("SELECT extraction_hash FROM document_link_candidates")) == (other_hash,)


def test_amount_rule_upgrade_reuses_retained_text_once_without_upgrading_document_receipt(tmp_path):
    index, raw = _index(tmp_path)
    item = fixtures._disclosure("reviewed", "关于签订重大合同的公告")
    _stage(index, raw, item, "本公司已签订正式合同，合同已生效。协议总金额人民币2亿元（不含税）。" + _DETAIL * 2)
    index.publish(item.codes, NOW)
    facts = _facts(index)
    event = facts.positive_events[0]
    legacy = replace(facts, positive_events=(replace(event, measurements=replace(
        event.measurements, contract_amount_cny=None,
    )),))
    _publish_formal_fact(index, legacy)
    with SqliteSession(index.path) as collected, SqliteTransaction(collected):
        collected.run(
            "INSERT INTO company_field_repairs SELECT id,?,record_hash,?,'contract_amount',"
            "'explicit_cny_contract_amount:1','attempted' FROM disclosures WHERE source_key=?",
            (item.codes[0], event.event_id, item.identity),
        )
    with SqliteSession(index.path) as collected, SqliteSession(index.facts_path) as formal:
        plans = ReadCompanyFieldRecovery(formal, collected, item.codes, NOW, tmp_path)
        original_receipts = tuple(tuple(row) for row in formal.run(
            "SELECT * FROM company_input_receipt WHERE outcome='valid'"
        ))
    amount = next(plan for plan in plans if plan.gap.field == "contract_amount")
    assert amount.action == "extract"
    index.prepare_publication(item.codes, NOW + timedelta(minutes=2))
    assert _facts(index).positive_events[0].measurements.contract_amount_cny == 200_000_000
    for minute in (3, 4):
        restarted, _raw = _index(tmp_path)
        restarted.prepare_publication(item.codes, NOW + timedelta(minutes=minute))
        assert restarted.pending_documents(item.codes, NOW, policies=fixtures._TEST_DOCUMENT_POLICIES) == ()
    with SqliteSession(index.facts_path) as formal:
        assert tuple(tuple(row) for row in formal.run(
            "SELECT * FROM company_input_receipt WHERE outcome='valid'"
        )) == original_receipts
        assert formal.run(
            "SELECT COUNT(*) FROM company_input_receipt WHERE outcome='field_attempted' "
            "AND policy_id LIKE '%:contract_amount:explicit_cny_contract_amount:3'"
        ).require_one()[0] == 1
