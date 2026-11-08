"""Isolated task recovery preserves full file checks while omitting identical candidate writes."""

import hashlib
import os
import sqlite3
from datetime import datetime
from functools import partial
from pathlib import Path
from threading import Event
from zoneinfo import ZoneInfo

import pytest

from tests.helpers.download.company_resources import CompanyDocumentFilesFixture
from tests.helpers.download.configuration import CompanyEventPolicyFixture, CompanySyncConfigurationFixture
from tests.helpers.shared.configuration import SqlitePolicyFixture
from trader.download.domain.company_evidence import CompanyDocumentExtraction, Disclosure, DisclosurePage, DisclosurePartition
from trader.download.infra import company_document_recovery
from trader.download.infra.company_evidence_index import SQLiteCompanyEvidenceIndex
from trader.download.infra.company_raw_files import CompanyRawFiles
from trader.infra.sqlite_connection import SqliteConnection, SqliteSession, SqliteTransaction

_NOW = datetime(2026, 10, 18, 9, 15, tzinfo=ZoneInfo("Asia/Shanghai"))


def _index(root: Path, *, candidate: bool = False, cancelled=None) -> SQLiteCompanyEvidenceIndex:
    raw = CompanyRawFiles(root / "raw", capacity=1024**3, analysis_reserve=1024**2, free_reserve=1,
                          maximum_document_bytes=CompanySyncConfigurationFixture().extended_document_limits.max_bytes)
    build = partial(SQLiteCompanyEvidenceIndex, skip_unchanged_document_recovery=True) if candidate else SQLiteCompanyEvidenceIndex
    index = build(
        root / "company_evidence.sqlite3",
        facts_path=root / "company_facts.sqlite3",
        documents=CompanyDocumentFilesFixture(root / "text"),
        raw_files=raw,
        event_policy=CompanyEventPolicyFixture(),
        sqlite_policy=SqlitePolicyFixture(),
        cancel_requested=cancelled,
    )
    index.initialize()
    return index


def _seed(index: SQLiteCompanyEvidenceIndex, count: int):
    items = tuple(
        Disclosure(
            "cninfo", f"task-{number:03d}", ("600001",), "关于收到立案告知书的公告", _NOW,
            f"https://static.cninfo.com.cn/finalpage/task-{number}.pdf", f"record-{number}",
        )
        for number in range(count)
    )
    for offset in range(0, count, 30):
        batch = items[offset:offset + 30]
        index.commit_page(
            DisclosurePartition(
                "sse", _NOW.date(), _NOW.date(), page=offset // 30 + 1,
                expected_rows=count if offset else None, received_rows=offset,
            ),
            DisclosurePage(batch, count, offset + len(batch) < count),
            _NOW,
        )
    raw = CompanyRawFiles(index.path.parent / "raw", capacity=1024**3, analysis_reserve=1024**2, free_reserve=1,
                          maximum_document_bytes=CompanySyncConfigurationFixture().extended_document_limits.max_bytes)
    files = []
    rows = []
    for item in items:
        payload = b"%PDF-1.7\n" + item.identity.encode() + b"\n%%EOF\n"
        digest = hashlib.sha256(payload).digest()
        directory = raw.directory(item)
        directory.mkdir(parents=True)
        path = directory / f"{digest.hex()}.pdf"
        path.write_bytes(payload)
        files.append(path)
        rows.append((item.source, item.identity, item.digest, "downloaded", str(path.relative_to(index.path.parent)), digest, len(payload)))
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        connection.run_many("INSERT INTO document_tasks VALUES(?,?,?,?,?,?,?)", rows)
    return raw, items, tuple(files)


def _tasks(index: SQLiteCompanyEvidenceIndex):
    with SqliteSession(index.path) as connection:
        return tuple(tuple(row) for row in connection.run(
            "SELECT source,source_key,record_hash,stage,path,payload_hash,size "
            "FROM document_tasks WHERE source_key LIKE 'task-%' ORDER BY source_key"
        ))


def _receipts(index: SQLiteCompanyEvidenceIndex):
    with SqliteSession(index.facts_path) as connection:
        return tuple(tuple(row) for row in connection.run(
            "SELECT * FROM company_input_receipt ORDER BY source,source_key,code,outcome"
        ))


def _record_recovery_transactions(monkeypatch):
    begun = []

    class _ObservedTransaction(SqliteTransaction):
        def begin(self):
            begun.append(self)
            super().begin()

    monkeypatch.setattr(company_document_recovery, "SqliteTransaction", _ObservedTransaction)
    return begun


@pytest.mark.parametrize("count", (1, 64, 65))
@pytest.mark.parametrize("candidate", (False, True))
def test_identical_tasks_keep_default_writes_and_candidate_checks_every_original(tmp_path, monkeypatch, count, candidate):
    index = _index(tmp_path, candidate=candidate)
    _raw, _items, files = _seed(index, count)
    before = _tasks(index)
    original_digest = CompanyRawFiles.digest
    checked = []

    def digest(owner, path, *, check=None):
        checked.append(path)
        return original_digest(owner, path, check=check)

    monkeypatch.setattr(CompanyRawFiles, "digest", digest)
    transactions = _record_recovery_transactions(monkeypatch)
    index.prepare_publication(("600001",), _NOW)
    assert set(checked) == set(files) and len(checked) == count
    assert len(transactions) == (0 if candidate else count)
    assert _tasks(index) == before


@pytest.mark.parametrize("field,value", (
    ("stage", "pending"),
    ("path", "raw/previous.pdf"),
    ("payload_hash", b"x" * 32),
    ("size", 0),
))
def test_each_changed_field_is_restored_in_one_individual_transaction(tmp_path, monkeypatch, field, value):
    index = _index(tmp_path, candidate=True)
    _seed(index, 3)
    expected = _tasks(index)
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        connection.run(f"UPDATE document_tasks SET {field}=? WHERE source_key='task-001'", (value,))
    transactions = _record_recovery_transactions(monkeypatch)
    index.prepare_publication(("600001",), _NOW)
    assert _tasks(index) == expected
    assert len(transactions) == 1


@pytest.mark.parametrize("damage", ("missing", "same_size_rewrite"))
def test_missing_or_same_size_corrupt_original_cannot_take_the_unchanged_path(tmp_path, monkeypatch, damage):
    index = _index(tmp_path, candidate=True)
    _raw, _items, files = _seed(index, 3)
    path = files[1]
    if damage == "missing":
        path.unlink()
    else:
        metadata = path.stat()
        payload = path.read_bytes()
        path.write_bytes(bytes((payload[0] ^ 1,)) + payload[1:])
        os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
    transactions = _record_recovery_transactions(monkeypatch)
    index.prepare_publication(("600001",), _NOW)
    assert _tasks(index)[1][3:] == ("pending", None, None, None)
    assert len(transactions) == 1
    if damage == "same_size_rewrite":
        assert path.with_suffix(".corrupt").exists()
        assert (path.parent / "recovery.json").exists()


def test_candidate_compares_current_task_after_hashing_instead_of_an_old_snapshot(tmp_path, monkeypatch):
    index = _index(tmp_path, candidate=True)
    _seed(index, 1)
    expected = _tasks(index)
    original_local = CompanyRawFiles.local

    def changed(raw, item, *, check=None):
        local = original_local(raw, item, check=check)
        with SqliteSession(index.path) as connection, SqliteTransaction(connection):
            connection.run("UPDATE document_tasks SET stage='pending' WHERE source_key=?", (item.identity,))
        return local

    monkeypatch.setattr(CompanyRawFiles, "local", changed)
    transactions = _record_recovery_transactions(monkeypatch)
    index.prepare_publication(("600001",), _NOW)
    assert _tasks(index) == expected
    assert len(transactions) == 1


def test_cancelled_recovery_keeps_prior_commit_and_restart_finishes_remaining_tasks(tmp_path, monkeypatch):
    cancelled = Event()
    index = _index(tmp_path, candidate=True, cancelled=cancelled.is_set)
    _seed(index, 3)
    expected = _tasks(index)
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        connection.run("UPDATE document_tasks SET stage='pending'")
    original_local = CompanyRawFiles.local
    calls = 0

    def interrupt(raw, item, *, check=None):
        nonlocal calls
        local = original_local(raw, item, check=check)
        calls += 1
        if calls == 2:
            cancelled.set()
        return local

    monkeypatch.setattr(CompanyRawFiles, "local", interrupt)
    transactions = _record_recovery_transactions(monkeypatch)
    with pytest.raises(InterruptedError, match="company_sync_cancelled"):
        index.prepare_publication(("600001",), _NOW)
    assert len(transactions) == 1
    assert sorted(row[3] for row in _tasks(index)) == ["downloaded", "pending", "pending"]
    cancelled.clear()
    monkeypatch.setattr(CompanyRawFiles, "local", original_local)
    restarted = _index(tmp_path, candidate=True, cancelled=cancelled.is_set)
    transactions.clear()
    restarted.prepare_publication(("600001",), _NOW)
    assert len(transactions) == 2
    assert _tasks(restarted) == expected


def test_recovery_update_failure_rolls_back_and_leaves_originals_available_for_restart(tmp_path, monkeypatch):
    index = _index(tmp_path, candidate=True)
    _raw, _items, files = _seed(index, 1)
    expected = _tasks(index)
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        connection.run("UPDATE document_tasks SET stage='pending'")
    before = _tasks(index)
    contents = tuple(path.read_bytes() for path in files)
    original_run = SqliteConnection.run

    def fail(connection, sql, parameters=()):
        result = original_run(connection, sql, parameters)
        if sql.startswith("UPDATE document_tasks SET stage=?,path=?,payload_hash=?,size=?"):
            raise sqlite3.OperationalError("fixture_recovery_write_failure")
        return result

    monkeypatch.setattr(SqliteConnection, "run", fail)
    with pytest.raises(RuntimeError, match="company_evidence_persistence_unavailable") as failure:
        index.prepare_publication(("600001",), _NOW)
    assert isinstance(failure.value.__cause__, sqlite3.OperationalError)
    assert _tasks(index) == before
    assert tuple(path.read_bytes() for path in files) == contents
    monkeypatch.setattr(SqliteConnection, "run", original_run)
    restarted = _index(tmp_path, candidate=True)
    restarted.prepare_publication(("600001",), _NOW)
    assert _tasks(restarted) == expected


def test_candidate_recovery_preserves_existing_formal_receipts(tmp_path):
    index = _index(tmp_path, candidate=True)
    _raw, items, _files = _seed(index, 1)
    text = "本公司收到立案告知书，事项仍在调查中。" * 5
    extraction = CompanyDocumentExtraction(text, "text", hashlib.sha256(text.encode()).digest(), "test-text")
    assert index.commit_document(items[0], extraction, _NOW)
    index.publish(items[0].codes, _NOW)
    receipts = _receipts(index)
    assert receipts
    index.prepare_publication(items[0].codes, _NOW)
    assert _receipts(index) == receipts
