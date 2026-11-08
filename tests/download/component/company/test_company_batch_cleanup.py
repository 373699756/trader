"""Publication receipts bound disk use without losing confirmed or unresolved facts."""

import gzip
import hashlib
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

import pytest

from tests.helpers.download.company import company_evidence as fixtures
from trader.download.application import company_documents
from trader.download.application.company_documents import CompanyDocumentRun
from trader.download.application.company_evidence import CompanyEvidenceDependencies, SyncCompanyEvidence, _SyncState
from trader.download.application.company_operations import CompanyOperations
from trader.download.domain.company_evidence import CompanySyncConfiguration, DisclosurePage, DisclosurePartition
from trader.download.infra.company_raw_files import CompanyRawFiles
from trader.infra.sqlite_connection import SqliteConnectionOptions, SqliteSession, SqliteTransaction
from tests.helpers.download.company_resources import SQLiteCompanyEvidenceIndexFixture as SQLiteCompanyEvidenceIndex, CompanyDocumentFilesFixture as CompanyDocumentFiles, SQLiteCompanyEvidenceReaderFixture as SQLiteCompanyEvidenceReader

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
    assert tuple((tmp_path / "text").rglob("*.gz")), "Unverified conclusions still require their accepted material"


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


def test_later_discovery_of_old_case_links_saved_candidate_without_redownload(tmp_path):
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
    } == set()
    facts = reader.read(old.codes)[0]
    assert not facts.risk_events
    assert not any(item.reason_code == "resolution_unlinked" for item in facts.review_reasons)


@pytest.mark.parametrize("fill_missing", [False, True])
def test_rejected_late_result_cannot_replace_link_candidate_used_by_later_sync(tmp_path, fill_missing):
    index = fixtures._index(tmp_path)
    closure = fixtures._disclosure("closed", "关于调查终结的公告")
    _put(index, closure, "本公司收到证监会〔2026〕调查1号通知，调查终结。")
    index.publish(closure.codes, NOW)
    with SqliteSession(index.path) as connection:
        accepted = connection.run("SELECT payload FROM document_link_candidates").require_one()[0]
    retained = tuple((tmp_path / "text").rglob("*.gz"))
    assert len(retained) == 1, "An unlinked resolution must keep its accepted material"
    assert not index.commit_document(
        closure, fixtures._extraction("本公司收到证监会〔2026〕调查2号通知，调查终结。"), NOW + timedelta(seconds=1)
    )
    with SqliteSession(index.path) as connection:
        assert connection.run("SELECT payload FROM document_link_candidates").require_one()[0] == accepted
    assert tuple((tmp_path / "text").rglob("*.gz")) == retained
    opened = fixtures._disclosure("opened", "关于收到立案告知书的公告", when=NOW - timedelta(days=3))
    index.commit_page(
        DisclosurePartition("sse", opened.published_at.date(), opened.published_at.date()),
        DisclosurePage((opened,), 1, False), NOW,
    )

    extracted = []

    class Source(fixtures.Sources):
        def fetch(self, first, *args, **kwargs):
            if isinstance(first, DisclosurePartition):
                rows = tuple(
                    item for item in (opened, closure)
                    if first.start <= item.published_at.date() <= first.end
                )
                return DisclosurePage(rows, len(rows), False)
            return super().fetch(first, *args, **kwargs)

        def document(self, item, **kwargs):
            assert item.identity == opened.identity
            extracted.append(item.identity)
            return fixtures._extraction(_CASE)

    source = Source()
    result = SyncCompanyEvidence(
        CompanyEvidenceDependencies(index, source, source, lambda: 0.0),
        fixtures.CompanyEvidenceRequest(
            opened.codes, NOW, fixtures._TEST_COMPANY_CONFIGURATION, fill_missing=fill_missing,
        ),
    )
    assert not result.needs_retry
    assert extracted == [opened.identity]
    facts = SQLiteCompanyEvidenceReader(index.facts_path).read(opened.codes)[0]
    assert not facts.risk_events
    assert not any(item.reason_code == "resolution_unlinked" for item in facts.review_reasons)
    assert not tuple((tmp_path / "text").rglob("*.gz")), "Resolved evidence can be released after publication"


def test_full_text_quota_reuses_body_and_keeps_each_announcement_receipt(tmp_path, monkeypatch):
    index = fixtures._index(tmp_path)
    extraction = fixtures._extraction(_CASE)
    payload = gzip.compress(extraction.text.encode(), compresslevel=1, mtime=0)
    files = CompanyDocumentFiles(tmp_path / "text", max_bytes=len(payload), reserve_bytes=1)
    index._documents = files
    raw = CompanyRawFiles(
        tmp_path,
        capacity=1_000_000,
        analysis_reserve=100_000,
        free_reserve=1,
        maximum_document_bytes=65536,
    )
    index._raw_files = raw
    first = fixtures._disclosure("first", "关于收到立案告知书的公告", digest="first-version")
    second = fixtures._disclosure("second", "关于收到立案告知书的公告", digest="second-version")
    partition = DisclosurePartition("sse", NOW.date(), NOW.date())
    index.commit_page(partition, DisclosurePage((first, second), 2, False), NOW)
    assert index.commit_document(first, replace(extraction, payload_hash=b"a" * 32), NOW)
    path, = (tmp_path / "text").glob("*/*.txt.gz")
    before = path.stat()
    original = path.read_bytes()
    assert [task.disclosure.identity for task in index.pending_documents(
        second.codes, NOW, limit=1, policies=fixtures._TEST_DOCUMENT_POLICIES,
    )] == [second.identity]

    def unexpected_write(*args, **kwargs):
        pytest.fail("valid retained text must not compress, write, fsync or reserve new text space")

    with monkeypatch.context() as patch:
        patch.setattr(gzip, "compress", unexpected_write)
        patch.setattr(files, "_write", unexpected_write)
        patch.setattr(raw, "reserve", unexpected_write)
        assert files.write(extraction.text) == hashlib.sha256(extraction.text.encode()).digest()
        assert index.commit_document(second, replace(extraction, payload_hash=b"b" * 32), NOW)
    after = path.stat()
    assert (after.st_ino, after.st_mtime_ns, path.read_bytes()) == (before.st_ino, before.st_mtime_ns, original)
    with pytest.raises(RuntimeError, match="company_temporary_space_exhausted"):
        files.write(extraction.text + "新增内容")
    assert (path.stat().st_ino, path.stat().st_mtime_ns, path.read_bytes()) == (
        before.st_ino, before.st_mtime_ns, original,
    )
    with SqliteSession(index.path) as connection:
        rows = connection.run(
            "SELECT d.source_key,e.record_hash,e.payload_hash,d.body_hash FROM document_extractions e "
            "JOIN disclosures d ON d.id=e.disclosure_id ORDER BY d.source_key"
        ).fetchall()
        assert [(row[0], row[1], row[2]) for row in rows] == [
            (first.identity, first.digest, b"a" * 32), (second.identity, second.digest, b"b" * 32),
        ]
        assert rows[0][3] == rows[1][3]
    index.publish(first.codes, NOW)
    with SqliteSession(index.facts_path) as connection:
        receipts = connection.run(
            "SELECT source_key,record_hash,body_hash FROM company_input_receipt ORDER BY source_key"
        ).fetchall()
        assert [(row[0], row[1]) for row in receipts] == [
            (first.identity, first.digest), (second.identity, second.digest),
        ]
        assert receipts[0][2] == receipts[1][2] == hashlib.sha256(extraction.text.encode()).digest()


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


def test_text_capacity_failure_drains_other_accepted_results_and_stops_dispatch(tmp_path, monkeypatch):
    index = fixtures._index(tmp_path)
    index.prepare_publication(("600001",), NOW)
    retained = fixtures._extraction(_CASE)
    payload = gzip.compress(retained.text.encode(), compresslevel=1, mtime=0)
    files = CompanyDocumentFiles(tmp_path / "text", max_bytes=len(payload), reserve_bytes=1)
    files.write(retained.text)
    index._documents = files
    items = tuple(
        fixtures._disclosure(str(number), "关于收到立案告知书的公告", when=NOW - timedelta(seconds=number))
        for number in range(3)
    )
    index.commit_page(
        DisclosurePartition("sse", NOW.date(), NOW.date()), DisclosurePage(items, len(items), False), NOW,
    )
    extracted = []
    rejected = fixtures._extraction(_CASE + "新增内容")

    class Source(fixtures.Sources):
        def document_ready(self, item):
            return True

        def document(self, item, **kwargs):
            extracted.append(item.identity)
            return rejected if item.identity == items[0].identity else retained

    def fail_first_wait(futures, **kwargs):
        # Exercise failure before draining the other already completed result.
        ordered = tuple(futures)
        first = next((future for future in ordered if future.result() == rejected), ordered[0])
        return {first}, set(ordered) - {first}

    monkeypatch.setattr(company_documents, "wait", fail_first_wait)
    source = Source()
    events = []
    configuration = replace(fixtures._TEST_COMPANY_CONFIGURATION, text_workers=2, document_batch_size=3)
    dependencies = CompanyEvidenceDependencies(
        index,
        source,
        source,
        lambda: 0.0,
        events.append,
    )
    state = _SyncState(CompanyOperations(events.append, lambda: 0.0, (), population=items[0].codes), 1)
    run = CompanyDocumentRun(
        dependencies,
        items[0].codes,
        state,
        _DocumentContext(NOW, 0.0, configuration),
        publish=lambda: None,
    )
    run.run_batch()
    assert set(extracted) == {items[0].identity, items[1].identity}
    assert run.capacity_blocked and run.completed == 1
    assert state.failures == ("risk_documents:company_temporary_space_exhausted",)
    with SqliteSession(index.path) as connection:
        rows = connection.run("SELECT source_key,body_checked FROM disclosures ORDER BY source_key").fetchall()
        assert [(row[0], row[1]) for row in rows] == [(items[0].identity, 0), (items[1].identity, 1), (items[2].identity, 0)]
        assert connection.run("SELECT COUNT(*) FROM document_extractions").require_one()[0] == 1
        assert connection.run("SELECT COUNT(*) FROM document_retries").require_one()[0] == 0
    assert any(event.component == "risk_documents" and event.status == "deferred" for event in events)


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
