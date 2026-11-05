"""Legacy contract link payloads cannot bypass the current body subject guard."""

import hashlib
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from tests.helpers.configuration import CompanyDocumentFilesFixture, CompanyEventPolicyFixture
from trader.download.domain.company_event_extraction import ExtractCompanyLinkCandidate, ResolveCompanyLinks
from trader.download.domain.company_events import CompanyReviewReason
from trader.download.domain.company_evidence import CompanyDocument, Disclosure
from trader.download.infra.company_contract_link_rules import (
    ApplyCompanyContractLinkRefreshes,
    ReadCompanyContractLinkRules,
    WriteCompanyContractLinkRules,
)
from trader.download.infra.company_link_codec import EncodeCompanyLink
from trader.download.infra.company_publication_reads import PendingCompanyContractLinkCodes, ReadCompanyPublicationLinks
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction

_NOW = datetime(2026, 11, 5, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
_EXTRACTION = b"e" * 32
_REVENUE = "2025年度经审计的营业收入为人民币10000万元（不含税）。2025年年度报告于2026年4月20日披露。"
_ISSUER = "本公司就公告root披露进展，已签订正式合同。合同金额为人民币2000万元（不含税）。" + _REVENUE
_SUBSIDIARY = (
    "本公司及董事会全体成员保证信息披露的内容真实、准确、完整。"
    "公司全资子公司甲公司就公告root已签订正式合同。合同金额为人民币2000万元（不含税）。" + _REVENUE
)


@pytest.fixture
def connection():
    with SqliteSession(":memory:") as connection, SqliteTransaction(connection):
        connection.run(
            "CREATE TABLE disclosures(id INTEGER PRIMARY KEY,source TEXT,source_key TEXT,record_hash TEXT,"
            "body_hash BLOB,body_retained INTEGER,body_checked INTEGER,title TEXT,published_at TEXT,url TEXT)"
        )
        connection.run("CREATE TABLE disclosure_codes(disclosure_id INTEGER,code TEXT)")
        connection.run(
            "CREATE TABLE document_link_candidates(source TEXT,source_key TEXT,record_hash TEXT,extraction_hash BLOB,payload TEXT,"
            "PRIMARY KEY(source,source_key,record_hash,extraction_hash))"
        )
        yield connection


def _seed(connection, files, *, body=_ISSUER, codes=("600001",)):
    item = Disclosure("cninfo", "progress", codes, "关于签订重大合同的进展公告", _NOW, "", "record")
    # This typed issuer payload represents what the former parser could derive
    # even when the actual stored body described a subsidiary instead.
    candidate = ExtractCompanyLinkCandidate(CompanyDocument(item, _ISSUER, True), _NOW, CompanyEventPolicyFixture())
    assert candidate is not None
    digest = files.write(body)
    connection.run(
        "INSERT INTO disclosures VALUES(1,?,?,?,?,1,1,?,?,?)",
        (item.source, item.identity, item.digest, digest, item.title, item.published_at.isoformat(), item.url),
    )
    connection.run_many("INSERT INTO disclosure_codes VALUES(1,?)", ((code,) for code in codes))
    connection.run(
        "INSERT INTO document_link_candidates VALUES(?,?,?,?,?)",
        (item.source, item.identity, item.digest, _EXTRACTION, EncodeCompanyLink(candidate)),
    )
    return item, candidate, digest


def _read(connection, files, codes=("600001",)):
    return ReadCompanyPublicationLinks(
        connection,
        codes,
        _EXTRACTION,
        observed_at=_NOW,
        event_policy=CompanyEventPolicyFixture(),
        read_document=files.read,
    )


def _missing(_digest):
    raise AssertionError("A proven cleaned candidate must not request material")


def test_legacy_contract_without_metadata_is_rechecked_then_remains_usable_after_normal_cleanup(connection, tmp_path):
    files = CompanyDocumentFilesFixture(tmp_path / "text")
    _item, candidate, _digest = _seed(connection, files)
    assert PendingCompanyContractLinkCodes(connection, ("600001",), _EXTRACTION) == ("600001",)
    result = _read(connection, files)[0]
    assert result.candidates == (candidate,) and result.reviews == () and len(result.refreshes) == 1
    ApplyCompanyContractLinkRefreshes(connection, result.refreshes)
    assert len(ReadCompanyContractLinkRules(connection, ("600001",), _EXTRACTION)) == 1
    assert PendingCompanyContractLinkCodes(connection, ("600001",), _EXTRACTION) == ()
    connection.run("UPDATE disclosures SET body_retained=0")
    result = ReadCompanyPublicationLinks(
        connection, ("600001",), _EXTRACTION,
        observed_at=_NOW, event_policy=CompanyEventPolicyFixture(), read_document=_missing,
    )[0]
    assert result.candidates == (candidate,) and result.reviews == () and result.refreshes == ()


def test_legacy_subsidiary_payload_cannot_resolve_old_progress_review_or_replace_issuer_event(connection, tmp_path):
    files = CompanyDocumentFilesFixture(tmp_path / "text")
    _item, old_candidate, _digest = _seed(connection, files, body=_SUBSIDIARY)
    result = _read(connection, files)[0]
    assert result.candidates == ()
    assert tuple(review.reason_code for review in result.reviews) == ("subject_unverified",)
    root = replace(old_candidate.positive, event_id="root", evidence_ids=("root",))
    review = CompanyReviewReason("positive", "progress_unlinked", "progress", _NOW, "旧进展待关联")
    resolved = ResolveCompanyLinks(result.candidates, (), (root,), (review, *result.reviews), CompanyEventPolicyFixture())
    assert resolved.positives == (root,) and review in resolved.reviews
    ApplyCompanyContractLinkRefreshes(connection, result.refreshes)
    assert connection.run("SELECT count(*) FROM document_link_candidates").require_one()[0] == 0
    assert ReadCompanyContractLinkRules(connection, ("600001",), _EXTRACTION) == frozenset()


def test_legacy_missing_material_is_not_blessed_and_can_be_rechecked_when_local_material_returns(connection, tmp_path):
    files = CompanyDocumentFilesFixture(tmp_path / "text")
    _item, candidate, _digest = _seed(connection, files)
    connection.run("UPDATE disclosures SET body_retained=0")
    result = _read(connection, files)[0]
    assert result.candidates == () and result.refreshes == ()
    assert tuple(review.reason_code for review in result.reviews) == ("subject_unverified",)
    assert PendingCompanyContractLinkCodes(connection, ("600001",), _EXTRACTION) == ("600001",)
    connection.run("UPDATE disclosures SET body_retained=1")
    result = _read(connection, files)[0]
    assert result.candidates == (candidate,) and result.reviews == () and result.refreshes


@pytest.mark.parametrize("column,value", (
    ("source", "other-source"), ("source_key", "other-key"), ("code", "600002"),
    ("record_hash", "other-record"), ("body_hash", b"b" * 32), ("extraction_hash", b"x" * 32),
    ("payload_hash", b"p" * 32), ("rule_id", "old-rule"),
))
def test_local_proof_requires_every_source_version_code_body_payload_and_rule_binding(connection, tmp_path, column, value):
    files = CompanyDocumentFilesFixture(tmp_path / "text")
    item, candidate, digest = _seed(connection, files)
    WriteCompanyContractLinkRules(connection, item, digest, _EXTRACTION, candidate)
    connection.run(f"UPDATE document_contract_link_rules SET {column}=?", (value,))
    connection.run("UPDATE disclosures SET body_retained=0")
    result = _read(connection, files)[0]
    assert result.candidates == () and result.refreshes == ()
    assert result.reviews[0].reason_code == "subject_unverified"
    assert PendingCompanyContractLinkCodes(connection, ("600001",), _EXTRACTION) == ("600001",)


def test_payload_change_cannot_reuse_proof_for_a_previously_valid_contract(connection, tmp_path):
    files = CompanyDocumentFilesFixture(tmp_path / "text")
    item, candidate, digest = _seed(connection, files)
    WriteCompanyContractLinkRules(connection, item, digest, _EXTRACTION, candidate)
    changed = replace(candidate, positive=replace(candidate.positive, measurements=replace(candidate.positive.measurements, contract_amount_cny=99_000_000)))
    connection.run("UPDATE document_link_candidates SET payload=?", (EncodeCompanyLink(changed),))
    connection.run("UPDATE disclosures SET body_retained=0")
    assert _read(connection, files)[0].candidates == ()


def test_hash_mismatch_never_creates_current_rule_metadata(connection, tmp_path):
    files = CompanyDocumentFilesFixture(tmp_path / "text")
    _seed(connection, files)
    result = ReadCompanyPublicationLinks(
        connection, ("600001",), _EXTRACTION,
        observed_at=_NOW, event_policy=CompanyEventPolicyFixture(), read_document=lambda _digest: "wrong body",
    )[0]
    assert result.candidates == () and result.refreshes == () and result.reviews
    assert ReadCompanyContractLinkRules(connection, ("600001",), _EXTRACTION) == frozenset()


@pytest.mark.parametrize("failure", (OSError, ValueError, EOFError))
def test_unreadable_legacy_material_becomes_review_without_a_global_failure(connection, tmp_path, failure):
    files = CompanyDocumentFilesFixture(tmp_path / "text")
    _seed(connection, files)

    def read(_digest):
        raise failure("isolated missing or invalid material")

    result = ReadCompanyPublicationLinks(
        connection, ("600001",), _EXTRACTION,
        observed_at=_NOW, event_policy=CompanyEventPolicyFixture(), read_document=read,
    )[0]
    assert result.candidates == () and result.refreshes == ()
    assert result.reviews[0].reason_code == "subject_unverified"


def test_current_body_disagreement_blocks_old_fact_without_overwriting_its_measurements(connection, tmp_path):
    files = CompanyDocumentFilesFixture(tmp_path / "text")
    _item, candidate, _digest = _seed(connection, files)
    changed = replace(candidate, positive=replace(candidate.positive, measurements=replace(candidate.positive.measurements, contract_amount_cny=99_000_000)))
    connection.run("UPDATE document_link_candidates SET payload=?", (EncodeCompanyLink(changed),))
    result = _read(connection, files)[0]
    assert result.candidates == (candidate,) and result.reviews[0].reason_code == "subject_unverified"
    resolved = ResolveCompanyLinks(result.candidates, (), (changed.positive,), result.reviews, CompanyEventPolicyFixture())
    assert resolved.positives[0].measurements == changed.positive.measurements
    assert resolved.positives[0].positive_level == 0 and resolved.positives[0].not_eligible_reason == "revision_pending"


@pytest.mark.parametrize("change", ("record_hash='new-record'", "body_hash=zeroblob(32)", "body_checked=0"))
def test_late_local_refresh_cannot_replace_a_changed_current_source(connection, tmp_path, change):
    files = CompanyDocumentFilesFixture(tmp_path / "text")
    _seed(connection, files)
    refreshes = _read(connection, files)[0].refreshes
    original = connection.run("SELECT payload FROM document_link_candidates").require_one()[0]
    connection.run(f"UPDATE disclosures SET {change}")
    ApplyCompanyContractLinkRefreshes(connection, refreshes)
    assert connection.run("SELECT payload FROM document_link_candidates").require_one()[0] == original
    assert ReadCompanyContractLinkRules(connection, ("600001",), _EXTRACTION) == frozenset()


def test_refresh_does_not_overwrite_another_new_payload(connection, tmp_path):
    files = CompanyDocumentFilesFixture(tmp_path / "text")
    _item, candidate, _digest = _seed(connection, files)
    refreshes = _read(connection, files)[0].refreshes
    changed = EncodeCompanyLink(replace(candidate, references=("another-root",)))
    connection.run("UPDATE document_link_candidates SET payload=?", (changed,))
    ApplyCompanyContractLinkRefreshes(connection, refreshes)
    assert connection.run("SELECT payload FROM document_link_candidates").require_one()[0] == changed
    assert ReadCompanyContractLinkRules(connection, ("600001",), _EXTRACTION) == frozenset()


def test_one_physical_body_is_rechecked_once_but_each_code_gets_its_own_proof(connection, tmp_path):
    files = CompanyDocumentFilesFixture(tmp_path / "text")
    codes = ("600001", "600002")
    _seed(connection, files, codes=codes)
    calls = []

    def read(digest):
        calls.append(digest)
        return files.read(digest)

    results = ReadCompanyPublicationLinks(
        connection, codes, _EXTRACTION, observed_at=_NOW, event_policy=CompanyEventPolicyFixture(), read_document=read,
    )
    assert len(calls) == 1 and all(result.candidates for result in results)
    ApplyCompanyContractLinkRefreshes(connection, tuple(refresh for result in results for refresh in result.refreshes))
    assert len(ReadCompanyContractLinkRules(connection, codes, _EXTRACTION)) == 2
    assert PendingCompanyContractLinkCodes(connection, codes, _EXTRACTION) == ()


def test_noncontract_candidates_do_not_require_new_contract_metadata_or_material(connection, tmp_path):
    files = CompanyDocumentFilesFixture(tmp_path / "text")
    item, _candidate, _digest = _seed(connection, files)
    item = replace(item, title="关于回购股份进展的公告")
    candidate = ExtractCompanyLinkCandidate(
        CompanyDocument(item, "本公司累计已回购股份，占公司总股本的0.2%，回购用于注销。", True), _NOW, CompanyEventPolicyFixture()
    )
    assert candidate is not None
    connection.run("UPDATE document_link_candidates SET payload=?", (EncodeCompanyLink(candidate),))
    connection.run("UPDATE disclosures SET body_retained=0")
    result = ReadCompanyPublicationLinks(
        connection, item.codes, _EXTRACTION, observed_at=_NOW, event_policy=CompanyEventPolicyFixture(), read_document=_missing,
    )[0]
    assert result.candidates == (candidate,) and not result.reviews and not result.refreshes
    assert PendingCompanyContractLinkCodes(connection, item.codes, _EXTRACTION) == ()


def test_rule_writer_rejects_an_unassociated_code_and_wrong_body_hash(connection, tmp_path):
    files = CompanyDocumentFilesFixture(tmp_path / "text")
    item, candidate, digest = _seed(connection, files)
    WriteCompanyContractLinkRules(connection, replace(item, codes=("600002",)), digest, _EXTRACTION, candidate)
    WriteCompanyContractLinkRules(connection, item, hashlib.sha256(b"wrong body").digest(), _EXTRACTION, candidate)
    assert not ReadCompanyContractLinkRules(connection, ("600001", "600002"), _EXTRACTION)
