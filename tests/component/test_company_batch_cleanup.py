"""Publication receipts bound disk use without losing confirmed or unresolved facts."""

from dataclasses import replace
from datetime import timedelta

import pytest

from tests.component import test_company_evidence as fixtures
from trader.download.domain.company_evidence import DisclosurePage, DisclosurePartition
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction
from tests.helpers.configuration import (
    SQLiteCompanyEvidenceIndexFixture as SQLiteCompanyEvidenceIndex,
    CompanyDocumentFilesFixture as CompanyDocumentFiles,
    SQLiteCompanyEvidenceReaderFixture as SQLiteCompanyEvidenceReader,
)

NOW = fixtures.NOW
_CASE = "本公司收到证监会立案告知书〔2026〕调查1号，决定立案。"


def _put(index, item, body):
    partition = DisclosurePartition("sse", item.published_at.date(), item.published_at.date())
    index.commit_page(partition, DisclosurePage((item,), 1, False), NOW)
    assert index.commit_document(item, fixtures._extraction(body), NOW)


def test_committed_text_is_deleted_and_facts_survive_raw_unavailability(tmp_path):
    index = fixtures._index(tmp_path)
    item = fixtures._disclosure("case", "关于收到立案告知书的公告")
    _put(index, item, _CASE)
    assert tuple((tmp_path / "text").rglob("*.gz"))
    index.publish(("600001",), NOW)
    assert not tuple((tmp_path / "text").rglob("*.gz"))
    reader = SQLiteCompanyEvidenceReader(index.facts_path, raw_path=index.path)
    before = reader.read(("600001",))
    assert before[0].risk_events[0].risk_level == 4
    with SqliteSession(index.path) as connection:
        assert tuple(connection.run("SELECT body_checked,body_retained FROM disclosures").require_one()) == (1, 0)
        assert connection.run("SELECT COUNT(*) FROM document_publications").require_one()[0] == 1
    index.path.unlink()
    assert reader.read(("600001",)) == before
    with pytest.raises(RuntimeError, match="links_unavailable"):
        reader.document_links("600001", ("case",))


def test_unresolved_review_survives_cleanup_restart_and_later_publication(tmp_path):
    index = fixtures._index(tmp_path)
    item = fixtures._disclosure("ambiguous", "关于收到立案告知书的公告")
    _put(index, item, "其它主体收到相关通知，无法确认是否为本公司。")
    index.publish(("600001",), NOW)
    reader = SQLiteCompanyEvidenceReader(index.facts_path)
    before = tuple(item for item in reader.read(("600001",))[0].review_reasons if item.evidence_id)
    assert before
    resumed = SQLiteCompanyEvidenceIndex(
        index.path, facts_path=index.facts_path, documents=CompanyDocumentFiles(tmp_path / "text")
    )
    resumed.initialize()
    resumed.prepare_publication(("600001",), NOW + timedelta(minutes=1))
    assert resumed.pending_documents(("600001",), NOW, policies=fixtures._TEST_DOCUMENT_POLICIES) == ()
    resumed.publish(("600001",), NOW + timedelta(minutes=1))
    assert tuple(item for item in reader.read(("600001",))[0].review_reasons if item.evidence_id) == before
    assert not tuple((tmp_path / "text").rglob("*.gz"))


def test_shared_document_waits_for_every_requested_issuer_before_cleanup(tmp_path):
    index = fixtures._index(tmp_path)
    index.prepare_publication(("600001", "600002"), NOW)
    item = replace(fixtures._disclosure("shared", "关于收到立案告知书的公告"), codes=("600001", "600002"))
    _put(index, item, _CASE)
    index.publish(("600001",), NOW)
    assert tuple((tmp_path / "text").rglob("*.gz"))
    index.publish(("600002",), NOW)
    assert not tuple((tmp_path / "text").rglob("*.gz"))
    assert all(row.risk_events for row in SQLiteCompanyEvidenceReader(index.facts_path).read(item.codes))


def test_failed_formal_commit_does_not_delete_input_and_retry_cleans_it(tmp_path):
    index = fixtures._index(tmp_path)
    item = fixtures._disclosure("case", "关于收到立案告知书的公告")
    _put(index, item, _CASE)
    with SqliteSession(index.facts_path) as connection, SqliteTransaction(connection):
        connection.run(
            "CREATE TRIGGER refuse_commit BEFORE INSERT ON company_publication BEGIN SELECT RAISE(ABORT,'fixture'); END"
        )
    with pytest.raises(RuntimeError, match="persistence_unavailable"):
        index.publish(item.codes, NOW)
    assert tuple((tmp_path / "text").rglob("*.gz"))
    assert SQLiteCompanyEvidenceReader(index.facts_path).read(item.codes) == ()
    with SqliteSession(index.facts_path) as connection:
        connection.run("DROP TRIGGER refuse_commit")
    index.publish(item.codes, NOW)
    assert not tuple((tmp_path / "text").rglob("*.gz"))


def test_revised_ordinary_title_keeps_locator_for_existing_formal_evidence(tmp_path):
    index = fixtures._index(tmp_path)
    item = fixtures._disclosure("case", "关于收到立案告知书的公告")
    _put(index, item, _CASE)
    index.publish(item.codes, NOW)
    revised = replace(item, title="更正公告", digest="revised")
    partition = DisclosurePartition("sse", item.published_at.date(), item.published_at.date())
    index.commit_page(partition, DisclosurePage((revised,), 1, False), NOW)
    index.publish(item.codes, NOW)
    reader = SQLiteCompanyEvidenceReader(index.facts_path, raw_path=index.path)
    assert reader.read(item.codes)[0].risk_events
    assert reader.document_links("600001", ("case",))


def test_later_discovery_of_old_case_requeues_unlinked_closure(tmp_path):
    index = fixtures._index(tmp_path)
    closure = fixtures._disclosure("closed", "关于调查终结的公告")
    text = "本公司收到证监会〔2026〕调查1号通知，调查终结。"
    _put(index, closure, text)
    index.publish(closure.codes, NOW)
    reader = SQLiteCompanyEvidenceReader(index.facts_path)
    assert any(item.reason_code == "resolution_unlinked" for item in reader.read(closure.codes)[0].review_reasons)
    old = fixtures._disclosure("opened", "关于收到立案告知书的公告", when=NOW - timedelta(days=3))
    _put(index, old, _CASE)
    index.publish(old.codes, NOW)
    assert {
        task.disclosure.identity
        for task in index.pending_documents(old.codes, NOW, policies=fixtures._TEST_DOCUMENT_POLICIES)
    } == {"closed"}
    assert index.commit_document(closure, fixtures._extraction(text), NOW)
    index.publish(old.codes, NOW)
    facts = reader.read(old.codes)[0]
    assert not facts.risk_events
    assert not any(item.reason_code == "resolution_unlinked" for item in facts.review_reasons)


def test_capacity_limits_batch_and_refuses_dispatch_when_no_document_fits(tmp_path):
    files = CompanyDocumentFiles(tmp_path / "text", max_text_characters=100, max_bytes=10_000, reserve_bytes=1)
    assert files.batch_limit(50) == 2
    full = CompanyDocumentFiles(tmp_path / "full", max_bytes=1, reserve_bytes=1)
    with pytest.raises(RuntimeError, match="temporary_space_exhausted"):
        full.batch_limit(50)
