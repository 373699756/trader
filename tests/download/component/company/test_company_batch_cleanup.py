"""Publication receipts bound disk use without losing confirmed or unresolved facts."""

import gzip
import hashlib
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime

import pytest
import sqlite3

from tests.helpers.download.company import company_evidence as fixtures
from trader.download.domain.company_evidence import CompanySyncConfiguration, DisclosurePage, DisclosurePartition
from trader.infra.sqlite_connection import SqliteConnectionOptions, SqliteSession, SqliteTransaction
from tests.helpers.download.company_resources import CompanyDocumentFilesFixture as CompanyDocumentFiles, SQLiteCompanyEvidenceReaderFixture as SQLiteCompanyEvidenceReader

NOW = fixtures.NOW
_CASE = "本公司收到证监会立案告知书〔2026〕调查1号，决定立案。"


def _put(index, item, body):
    partition = DisclosurePartition("sse", item.published_at.date(), item.published_at.date())
    index.commit_page(partition, DisclosurePage((item,), 1, False), NOW)
    assert index.commit_document(item, fixtures._extraction(body), NOW)


def test_committed_text_is_deleted_and_facts_survive_raw_unavailability(tmp_path):
    index = fixtures._index(tmp_path)
    item = fixtures._disclosure("case", "关于收到立案告知书的公告", digest="a" * 64)
    _put(index, item, _CASE)
    assert tuple((tmp_path / "text").rglob("*.gz"))
    index.publish(("600001",), NOW)
    index.clean_material(NOW)
    assert not tuple((tmp_path / "text").rglob("*.gz"))
    reader = SQLiteCompanyEvidenceReader(index.facts_path)
    before = reader.read(("600001",))
    assert before[0].risk_events[0].risk_level == 4
    with SqliteSession(index.path) as connection:
        assert tuple(connection.run("SELECT body_checked,body_retained FROM disclosures").require_one()) == (1, 0)
        assert connection.run("SELECT COUNT(*) FROM document_publications").require_one()[0] == 0
    index.path.unlink()
    assert reader.read(("600001",)) == before
    links = reader.read_document_links(("600001",))
    assert len(links.links) == 1 and links.links[0].link.evidence_id == "case"




def test_shared_document_waits_for_every_requested_issuer_before_cleanup(tmp_path):
    index = fixtures._index(tmp_path)
    index.prepare_and_publish(("600001", "600002"), NOW)
    item = replace(fixtures._disclosure("shared", "关于收到立案告知书的公告"), codes=("600001", "600002"))
    _put(index, item, _CASE)
    index.publish(("600001",), NOW)
    index.clean_material(NOW)
    assert tuple((tmp_path / "text").rglob("*.gz"))
    index.publish(("600002",), NOW)
    index.clean_material(NOW)
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
    with pytest.raises(sqlite3.IntegrityError, match="fixture"):
        index.publish(item.codes, NOW)
    assert tuple((tmp_path / "text").rglob("*.gz"))
    with pytest.raises(RuntimeError, match="company_snapshot_unreadable"):
        SQLiteCompanyEvidenceReader(index.facts_path).read(item.codes)
    with SqliteSession(index.facts_path) as connection:
        connection.run("DROP TRIGGER refuse_commit")
    assert SQLiteCompanyEvidenceReader(index.facts_path).read(item.codes) == ()
    index.publish(item.codes, NOW)
    index.clean_material(NOW)
    assert not tuple((tmp_path / "text").rglob("*.gz"))


def test_revised_ordinary_title_keeps_locator_for_existing_formal_evidence(tmp_path):
    index = fixtures._index(tmp_path)
    item = fixtures._disclosure("case", "关于收到立案告知书的公告", digest="a" * 64)
    _put(index, item, _CASE)
    index.publish(item.codes, NOW)
    revised = replace(item, title="更正公告", digest="b" * 64)
    partition = DisclosurePartition("sse", item.published_at.date(), item.published_at.date())
    index.commit_page(partition, DisclosurePage((revised,), 1, False), NOW)
    index.publish(item.codes, NOW)
    reader = SQLiteCompanyEvidenceReader(index.facts_path)
    assert reader.read(item.codes)[0].risk_events
    links = reader.read_document_links(("600001",)).links
    assert len(links) == 1 and links[0].record_hash == item.digest








@pytest.mark.parametrize("failure", ["gzip", "hash", "utf8"])
def test_invalid_retained_body_is_rejected_without_replacing_original(tmp_path, failure):
    files = CompanyDocumentFiles(tmp_path / "text", reserve_bytes=1)
    text = fixtures._extraction(_CASE).text
    raw = b"\xff" * 80 if failure == "utf8" else text.encode()
    digest = hashlib.sha256(raw).digest()
    path = files.root / digest.hex()[:2] / (digest.hex() + ".txt.gz")
    path.parent.mkdir(parents=True)
    payload = b"invalid-gzip" if failure == "gzip" else gzip.compress(
        b"wrong-body" if failure == "hash" else raw, compresslevel=1, mtime=0,
    )
    path.write_bytes(payload)
    before = path.stat()
    with pytest.raises((OSError, ValueError, EOFError)):
        if failure == "utf8":
            files.read(digest)
        else:
            with files.prepare(text):
                pytest.fail("damaged retained body must be preserved instead of replaced")
    assert (path.stat().st_ino, path.stat().st_mtime_ns, path.read_bytes()) == (
        before.st_ino, before.st_mtime_ns, payload,
    )


def test_nested_body_preparation_pins_material_until_last_context_exits(tmp_path):
    files = CompanyDocumentFiles(tmp_path / "text", reserve_bytes=1)
    text = fixtures._extraction(_CASE).text
    with files.prepare(text) as digest:
        path = files.root / digest.hex()[:2] / (digest.hex() + ".txt.gz")
        with files.prepare(text) as same:
            assert same == digest
            files.release(frozenset())
            assert path.exists()
        files.release(frozenset())
        assert path.exists()
    files.release(frozenset())
    assert not path.exists()


@pytest.mark.parametrize("revise_during_prepare", [False, True])
def test_body_preparation_releases_sql_writer_and_rechecks_exact_version(tmp_path, monkeypatch, revise_during_prepare):
    index = fixtures._index(tmp_path)
    item = fixtures._disclosure("version", "关于调查终结的公告")
    index.commit_page(
        DisclosurePartition("sse", NOW.date(), NOW.date()), DisclosurePage((item,), 1, False), NOW,
    )
    prepare = index._documents.prepare
    checked = []

    @contextmanager
    def prepare_with_writer(text, **kwargs):
        with prepare(text, **kwargs) as digest:
            with SqliteSession(
                index.path, SqliteConnectionOptions(timeout_seconds=0.1), sqlite_policy=index._sqlite_policy,
            ) as connection, SqliteTransaction(connection, immediate=True):
                checked.append(True)
                if revise_during_prepare:
                    connection.run("UPDATE disclosures SET record_hash='revision' WHERE source_key=?", (item.identity,))
            yield digest

    monkeypatch.setattr(index._documents, "prepare", prepare_with_writer)
    extraction = fixtures._extraction("本公司收到证监会〔2026〕调查1号通知，调查终结。")
    assert index.commit_document(item, extraction, NOW) is not revise_during_prepare
    assert checked == [True]
    with SqliteSession(index.path) as connection:
        row = connection.run("SELECT record_hash,body_checked,body_hash FROM disclosures").require_one()
        assert row[0] == ("revision" if revise_during_prepare else item.digest)
        assert row[1] == int(not revise_during_prepare)
        assert (row[2] is None) is revise_during_prepare
        assert connection.run("SELECT COUNT(*) FROM document_extractions").require_one()[0] == int(not revise_during_prepare)
        assert connection.run("SELECT COUNT(*) FROM document_link_candidates").require_one()[0] == int(not revise_during_prepare)


def test_body_preparation_releases_pin_after_sql_transaction_rolls_back(tmp_path):
    index = fixtures._index(tmp_path)
    item = fixtures._disclosure("rollback", "关于收到立案告知书的公告")
    index.commit_page(
        DisclosurePartition("sse", NOW.date(), NOW.date()), DisclosurePage((item,), 1, False), NOW,
    )
    with SqliteSession(index.path) as connection:
        connection.run(
            "CREATE TRIGGER reject_extraction BEFORE INSERT ON document_extractions "
            "BEGIN SELECT RAISE(ABORT,'fixture'); END"
        )
    with pytest.raises(RuntimeError, match="company_evidence_persistence_unavailable"):
        index.commit_document(item, fixtures._extraction(_CASE), NOW)
    paths = tuple((tmp_path / "text").glob("*/*.txt.gz"))
    assert len(paths) == 1
    with SqliteSession(index.path) as connection:
        assert tuple(connection.run("SELECT body_checked,body_hash FROM disclosures").require_one()) == (0, None)
        assert connection.run("SELECT COUNT(*) FROM document_extractions").require_one()[0] == 0
    index._documents.release(frozenset())
    assert not paths[0].exists(), "failed reference transaction must release its material pin"


@dataclass(frozen=True)
class _DocumentContext:
    observed_at: datetime
    started: float
    configuration: CompanySyncConfiguration






@pytest.mark.parametrize("boundary", ["compressed", "decompressed"])
def test_body_read_enforces_both_limits_and_preserves_oversized_material(tmp_path, boundary):
    files = CompanyDocumentFiles(tmp_path / "text", max_text_characters=1, reserve_bytes=1)
    raw = b"x" * 5
    digest = hashlib.sha256(raw).digest()
    path = files.root / digest.hex()[:2] / (digest.hex() + ".txt.gz")
    path.parent.mkdir(parents=True)
    payload = b"x" * 4101 if boundary == "compressed" else gzip.compress(raw, mtime=0)
    path.write_bytes(payload)
    with pytest.raises(ValueError, match="size_limit|integrity_invalid"):
        files.read(digest)
    assert path.read_bytes() == payload
