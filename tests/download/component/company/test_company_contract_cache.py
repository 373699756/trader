"""A missing contract cache proof restricts linkage without invalidating old facts."""

from dataclasses import replace
from datetime import timedelta
import hashlib

import pytest

from tests.helpers.download.company import company_evidence as fixtures
from tests.helpers.download.company_resources import CompanyDocumentFilesFixture, SQLiteCompanyEvidenceIndexFixture, SQLiteCompanyFactsIndexFixture
from trader.download.domain.company_events import CompanyPositiveStatus, CompanyReviewReason
from trader.download.domain.company_evidence import CompanyListing, DisclosurePage, DisclosurePartition
from trader.download.infra.company_fact_rows import ReadCompanyFactRows
from trader.download.infra.company_facts_index import CompanyInputReceipt, ReadCompanyPublicationIdentity
from trader.download.infra.company_field_result_rows import ReadCompanyFieldResults
from trader.download.infra.company_link_codec import DecodeCompanyLink, EncodeCompanyLink
from trader.download.infra.company_publication_reads import PendingCompanyContractLinkCodes
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction

_NOW = fixtures.NOW
_MEASUREMENTS = (
    "合同金额为人民币2000万元（不含税）。"
    "2025年度经审计的营业收入为人民币10000万元（不含税）。"
    "2025年年度报告于2026年4月20日披露。"
)
_ISSUER = "本公司已签订正式合同，合同已生效。" + _MEASUREMENTS
_SUBSIDIARY = (
    "本公司及董事会全体成员保证信息披露的内容真实、准确、完整。"
    "公司全资子公司甲公司已签订正式合同，合同已生效。" + _MEASUREMENTS
)


def _index(root, *, batch=False):
    index = SQLiteCompanyEvidenceIndexFixture(
        root / "company_evidence.sqlite3",
        facts_path=root / "company_facts.sqlite3",
        documents=CompanyDocumentFilesFixture(root / "text"),
        listings=(CompanyListing("600001", _NOW.date() - timedelta(days=365)),),
        batch_publication_reads=batch,
    )
    index.initialize()
    return index


def _put(index, item, body):
    partition = DisclosurePartition("sse", item.published_at.date(), item.published_at.date())
    index.commit_page(partition, DisclosurePage((item,), 1, False), _NOW)
    assert index.commit_document(item, fixtures._extraction(body), _NOW)


def _formal_state(index, source_key):
    with SqliteSession(index.facts_path) as formal:
        return (
            ReadCompanyFactRows(formal, ("600001",))[0],
            ReadCompanyPublicationIdentity(formal),
            tuple(tuple(row) for row in formal.run(
                "SELECT * FROM company_input_receipt WHERE source='cninfo' AND source_key=? AND outcome='valid' ORDER BY code",
                (source_key,),
            )),
        )


def _remove_cache_proof(index, item, *, remove_material):
    with SqliteSession(index.path) as collected, SqliteTransaction(collected):
        collected.run(
            "DELETE FROM document_contract_link_rules WHERE source=? AND source_key=? AND record_hash=?",
            (item.source, item.identity, item.digest),
        )
        if remove_material:
            collected.run(
                "UPDATE disclosures SET body_retained=0 WHERE source=? AND source_key=? AND record_hash=?",
                (item.source, item.identity, item.digest),
            )
    if remove_material:
        CompanyDocumentFilesFixture(index.path.parent / "text").release(frozenset())


@pytest.mark.parametrize("batch", (False, True), ids=("per-code", "batch"))
def test_unused_legacy_cache_without_material_keeps_published_contract_and_valid_receipt(tmp_path, batch):
    index = _index(tmp_path, batch=batch)
    item = fixtures._disclosure("contract", "关于签订重大合同的公告")
    _put(index, item, _ISSUER)
    index.publish(item.codes, _NOW)
    before, publication, valid = _formal_state(index, item.identity)
    assert before.positive_events[0].positive_level == 3 and valid
    assert not any(review.scope == "positive" for review in before.review_reasons)
    _remove_cache_proof(index, item, remove_material=True)

    index.prepare_publication(item.codes, _NOW)
    index.publish(item.codes, _NOW, changed_only=True)

    after, next_publication, next_valid = _formal_state(index, item.identity)
    assert after == before and next_publication == publication and next_valid == valid
    assert not index.pending_documents(item.codes, _NOW, policies=fixtures._TEST_DOCUMENT_POLICIES)
    with SqliteSession(index.path) as collected:
        assert tuple(collected.run("SELECT body_checked,body_retained FROM disclosures").require_one()) == (1, 0)
        assert collected.run("SELECT count(*) FROM document_contract_link_rules").require_one()[0] == 0


def test_missing_legacy_material_cannot_complete_unlinked_progress_when_original_arrives(tmp_path):
    index = _index(tmp_path, batch=True)
    progress = fixtures._disclosure("progress", "关于签订重大合同的进展公告")
    _put(index, progress, "本公司就公告original披露进展，已签订正式合同，合同已生效。" + _MEASUREMENTS)
    index.publish(progress.codes, _NOW)
    before, _publication, valid = _formal_state(index, progress.identity)
    assert before.positive_events == ()
    assert any(review.reason_code == "progress_unlinked" for review in before.review_reasons)
    with SqliteSession(index.path) as collected:
        candidate = DecodeCompanyLink(collected.run("SELECT payload FROM document_link_candidates").require_one()[0])
        assert "original" in candidate.references
    _remove_cache_proof(index, progress, remove_material=True)
    index.prepare_publication(progress.codes, _NOW)
    original = fixtures._disclosure("original", "关于签订重大合同的公告", when=_NOW - timedelta(days=1))
    _put(index, original, _ISSUER)

    index.publish(original.codes, _NOW, changed_only=True)

    after, _next_publication, next_valid = _formal_state(index, progress.identity)
    assert len(after.positive_events) == 1
    assert after.positive_events[0].evidence_ids == (original.identity,)
    assert after.positive_events[0].last_disclosed_at == original.published_at
    assert any(review.reason_code == "progress_unlinked" and review.evidence_id == progress.identity for review in after.review_reasons)
    assert any(review.reason_code == "subject_unverified" and review.evidence_id == progress.identity for review in after.review_reasons)
    assert next_valid == valid


def test_local_body_subject_conflict_still_blocks_legacy_issuer_fact_without_rewriting_valid(tmp_path):
    index = _index(tmp_path, batch=True)
    item = fixtures._disclosure("contract", "关于签订重大合同的公告")
    _put(index, item, _ISSUER)
    index.publish(item.codes, _NOW)
    legacy, parent, _valid = _formal_state(index, item.identity)
    assert legacy.positive_events[0].positive_level == 3
    # Model a former parser's incorrect issuer projection against its actual
    # subsidiary body. Seed this legacy state only in the isolated databases.
    body_hash = CompanyDocumentFilesFixture(tmp_path / "text").write(_SUBSIDIARY)
    formal_index = SQLiteCompanyFactsIndexFixture(index.facts_path)
    formal_index.publish(
        (legacy,), {item.identity: item.source}, parent, _NOW,
        receipts=(CompanyInputReceipt(
            item.source, item.identity, item.codes[0], item.digest, formal_index.extraction_hash, body_hash, _NOW,
        ),),
    )
    with SqliteSession(index.path) as collected, SqliteTransaction(collected):
        collected.run(
            "UPDATE disclosures SET body_hash=?,body_retained=1,body_checked=1 WHERE source=? AND source_key=? AND record_hash=?",
            (body_hash, item.source, item.identity, item.digest),
        )
    _remove_cache_proof(index, item, remove_material=False)
    before, _publication, valid = _formal_state(index, item.identity)
    assert not any(review.scope == "positive" for review in before.review_reasons)

    index.prepare_publication(item.codes, _NOW)
    index.publish(item.codes, _NOW, changed_only=True)

    after, _next_publication, next_valid = _formal_state(index, item.identity)
    assert len(after.positive_events) == 1
    assert after.positive_events[0].event_id == before.positive_events[0].event_id
    assert after.positive_events[0].status is CompanyPositiveStatus.IMPLEMENTING
    assert after.positive_events[0].positive_level == 0
    assert after.positive_events[0].not_eligible_reason == "revision_pending"
    assert after.positive_events[0].measurements == before.positive_events[0].measurements
    assert after.positive_events[0].level_milestones == before.positive_events[0].level_milestones
    assert any(review.reason_code == "subject_unverified" and review.evidence_id == item.identity for review in after.review_reasons)
    assert next_valid == valid
    with SqliteSession(index.path) as collected:
        assert collected.run("SELECT count(*) FROM document_link_candidates").require_one()[0] == 0
        assert collected.run("SELECT count(*) FROM document_contract_link_rules").require_one()[0] == 0


def test_same_body_amount_rule_upgrade_completes_old_fact_and_cache_without_subject_blocker(tmp_path):
    index = _index(tmp_path, batch=True)
    item = fixtures._disclosure("contract", "关于签订重大合同的公告")
    body = _ISSUER.replace("合同金额", "协议总金额", 1)
    _put(index, item, body)
    index.publish(item.codes, _NOW)
    current, parent, valid = _formal_state(index, item.identity)
    event = current.positive_events[0]
    missing = replace(event.measurements, contract_amount_cny=None, amount_to_revenue_pct=None, amount_basis="unknown")
    old_event = replace(event, measurements=missing, positive_level=0, level_milestones=(),
                        not_eligible_reason="implementation_or_measurement_unverified")
    old_review = CompanyReviewReason("positive", "implementation_or_measurement_unverified", item.identity, _NOW, "旧规则未取到金额")
    legacy = replace(current, positive_events=(old_event,), review_reasons=(*current.review_reasons, old_review))
    SQLiteCompanyFactsIndexFixture(index.facts_path).publish((legacy,), {item.identity: item.source}, parent, _NOW)
    body_hash = CompanyDocumentFilesFixture(tmp_path / "text").write(fixtures._extraction(body).text)
    with SqliteSession(index.path) as collected, SqliteTransaction(collected):
        candidate = DecodeCompanyLink(collected.run("SELECT payload FROM document_link_candidates").require_one()[0])
        old_candidate = replace(candidate, positive=replace(candidate.positive, measurements=missing, positive_level=0,
                                level_milestones=(), not_eligible_reason="implementation_or_measurement_unverified"))
        collected.run("UPDATE document_link_candidates SET payload=?", (EncodeCompanyLink(old_candidate),))
        collected.run("UPDATE disclosures SET body_hash=?,body_checked=1,body_retained=1", (body_hash,))
        collected.run(
            "INSERT INTO company_field_repairs SELECT id,?,record_hash,?,'contract_amount',"
            "'explicit_cny_contract_amount:1','attempted' FROM disclosures WHERE source_key=?",
            (item.codes[0], event.event_id, item.identity),
        )
    _remove_cache_proof(index, item, remove_material=False)

    index.prepare_publication(item.codes, _NOW)

    after, publication, next_valid = _formal_state(index, item.identity)
    assert after.positive_events[0].measurements == event.measurements
    assert after.positive_events[0].positive_level == 3 and after.positive_events[0].not_eligible_reason is None
    assert not any(review.scope == "positive" for review in after.review_reasons)
    assert publication.extraction_hash == parent.extraction_hash and next_valid == valid
    with SqliteSession(index.path) as collected:
        assert collected.run(
            "SELECT state FROM company_field_repairs WHERE field='contract_amount' AND rule_id='explicit_cny_contract_amount:3'"
        ).require_one()[0] == "complete"
        rebuilt = DecodeCompanyLink(collected.run("SELECT payload FROM document_link_candidates").require_one()[0])
        assert rebuilt.positive.positive_level == 3 and rebuilt.positive.measurements == event.measurements
    index.prepare_publication(item.codes, _NOW)
    index.publish(item.codes, _NOW, changed_only=True)
    assert _formal_state(index, item.identity) == (after, publication, valid)


def test_measurement_only_review_without_cache_material_does_not_create_permanent_subject_blocker(tmp_path):
    index = _index(tmp_path)
    item = fixtures._disclosure("contract", "关于签订重大合同的公告")
    _put(index, item, _ISSUER.replace("（不含税）", "（含税）", 1))
    index.publish(item.codes, _NOW)
    before, _publication, valid = _formal_state(index, item.identity)
    assert before.positive_events[0].positive_level == 0
    assert {review.reason_code for review in before.review_reasons if review.scope == "positive"} == {
        "implementation_or_measurement_unverified"
    }
    _remove_cache_proof(index, item, remove_material=True)

    index.prepare_publication(item.codes, _NOW)
    index.publish(item.codes, _NOW, changed_only=True)

    pending, _next_publication, next_valid = _formal_state(index, item.identity)
    assert next_valid == valid
    assert {review.reason_code for review in pending.review_reasons if review.scope == "positive"} == {
        "implementation_or_measurement_unverified"
    }
    progress = fixtures._disclosure("progress", "关于签订重大合同的进展公告")
    _put(index, progress, "本公司就公告contract披露进展，已签订正式合同，合同已生效。" + _MEASUREMENTS)
    index.publish(progress.codes, _NOW, changed_only=True)
    after, _final_publication, final_valid = _formal_state(index, item.identity)
    assert after.positive_events[0].evidence_ids == (item.identity, progress.identity)
    assert after.positive_events[0].positive_level == 3 and after.positive_events[0].not_eligible_reason is None
    assert not any(review.scope == "positive" for review in after.review_reasons)
    assert final_valid == valid


@pytest.mark.parametrize("batch", (False, True), ids=("per-code", "batch"))
@pytest.mark.parametrize("material_available", (False, True), ids=("missing-material", "retained-material"))
@pytest.mark.parametrize("stored_revenue", (False, True), ids=("no-old-revenue", "old-revenue-unavailable"))
def test_current_signing_rule_rechecks_exact_old_proof_without_rewriting_valid(tmp_path, monkeypatch, batch, material_available, stored_revenue):
    index = _index(tmp_path, batch=batch)
    item = fixtures._disclosure("signed-contract", "关于项目中标暨签署合同的自愿性信息披露公告")
    body = ("本公司及董事会全体成员保证信息披露真实、准确、完整。\n"
            "公司与业主方已正式签署相关合同。\n合同额：人民币3340000.00元。\n"
            "本次合同签约价占公司2025年度经审计营业收入的1.35%。")
    _put(index, item, body)
    index.publish(item.codes, _NOW)
    current, parent, valid = _formal_state(index, item.identity)
    event, = current.positive_events
    assert event.measurements.binding_confirmed and event.positive_level == 0
    old_event = replace(event, status=CompanyPositiveStatus.PLANNED,
                        measurements=replace(event.measurements, binding_confirmed=False,
                                             reference_annual_revenue_cny=1000000.0 if stored_revenue else None,
                                             reference_disclosed_at=_NOW - timedelta(days=100) if stored_revenue else None))
    legacy = replace(current, positive_events=(old_event,))
    with SqliteSession(index.facts_path) as formal:
        fields = ReadCompanyFieldResults(formal, item.codes)
    if stored_revenue:
        fields = tuple(replace(field, value=1000000.0 if field.field == "annual_revenue" else _NOW - timedelta(days=100),
                               state="present_unverified", reason="field_level_source_proof_missing")
                       if field.field in {"annual_revenue", "revenue_disclosed_at"} else field for field in fields)
    SQLiteCompanyFactsIndexFixture(index.facts_path).publish(
        (legacy,), {item.identity: item.source}, parent, _NOW, field_results=fields,
    )
    with SqliteSession(index.path) as collected, SqliteTransaction(collected):
        candidate = DecodeCompanyLink(collected.run("SELECT payload FROM document_link_candidates").require_one()[0])
        payload = EncodeCompanyLink(replace(candidate, positive=replace(
            event, status=CompanyPositiveStatus.PLANNED, measurements=replace(event.measurements, binding_confirmed=False),
        )))
        collected.run("UPDATE document_link_candidates SET payload=?", (payload,))
        collected.run("UPDATE document_contract_link_rules SET payload_hash=?,rule_id='contract_link_subject:1'",
                      (hashlib.sha256(payload.encode()).digest(),))
        if not material_available:
            collected.run("UPDATE disclosures SET body_retained=0")
    if not material_available:
        CompanyDocumentFilesFixture(tmp_path / "text").release(frozenset())
    before, old_publication, _valid = _formal_state(index, item.identity)
    with SqliteSession(index.facts_path) as formal:
        receipts = frozenset(tuple(row) for row in formal.run("SELECT * FROM company_input_receipt"))
    with SqliteSession(index.path) as collected:
        assert PendingCompanyContractLinkCodes(collected, item.codes, index._facts.extraction_hash) == item.codes

    index.prepare_publication(item.codes, _NOW)
    index.publish(item.codes, _NOW, changed_only=True)

    after, publication, next_valid = _formal_state(index, item.identity)
    assert next_valid == valid and publication.extraction_hash == parent.extraction_hash
    with SqliteSession(index.facts_path) as formal:
        assert ReadCompanyFieldResults(formal, item.codes) == fields
        next_receipts = frozenset(tuple(row) for row in formal.run("SELECT * FROM company_input_receipt"))
        assert receipts.issubset(next_receipts) and all(row[-2] == "field_attempted" for row in next_receipts - receipts)
    restored, = after.positive_events
    if material_available and not stored_revenue:
        assert restored.status is CompanyPositiveStatus.IMPLEMENTING and restored.measurements.binding_confirmed
        assert restored.measurements == event.measurements and restored.evidence_ids == old_event.evidence_ids
        assert restored.first_disclosed_at == old_event.first_disclosed_at and restored.last_disclosed_at == old_event.last_disclosed_at
        assert restored.positive_level == 0 and restored.level_milestones == ()
        assert restored.measurements.amount_to_revenue_pct is None and restored.measurements.amount_basis == "unknown"
        with SqliteSession(index.path) as collected:
            assert PendingCompanyContractLinkCodes(collected, item.codes, index._facts.extraction_hash) == ()
    else:
        assert after == before
        if material_available:
            assert stored_revenue and next_receipts - receipts
            assert all("annual_revenue" in row[-1] or "revenue_disclosed_at" in row[-1] for row in next_receipts - receipts)
        else:
            assert publication == old_publication

    restarted = _index(tmp_path, batch=batch)
    def unexpected_read(*args, **kwargs):
        raise AssertionError("a committed signing rule or unavailable material must not be reread")
    monkeypatch.setattr(restarted._documents, "read", unexpected_read)
    restarted.prepare_publication(item.codes, _NOW)
    restarted.publish(item.codes, _NOW, changed_only=True)
    assert _formal_state(restarted, item.identity) == (after, publication, valid)
