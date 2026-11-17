"""Exact proof-material recovery consumes each frozen request once and isolates every write."""

import hashlib
import os
from dataclasses import replace

import pytest

from scripts.download.company import company_document_download_audit as audit
from tests.helpers.download.company import company_evidence as fixtures
from tests.helpers.download.company.company_field_recovery import _put
from tests.helpers.shared.configuration import SqlitePolicyFixture
from trader.infra.atomic_files import AtomicWriteJson
from trader.infra.http_contracts import HttpResponse, HttpTimeout
from trader.infra.http_transport import RequestsHttpTransport
from trader.infra.sqlite_connection import SqliteSession
from trader.infra.process_lock import ProcessLock, ProcessLockError
from tests.helpers.download.company_resources import CompanyDocumentFilesFixture

_TEXT = (
    "证券代码：600001\n本公司控股股东拟减持股份，占公司总股本的比例为2%。\n"
    "减持期间为2026年10月28日至2026年11月28日。\n"
    "本公告仅说明本次事项的实际执行情况。相关安排已经正式披露，公司将根据实际执行情况及时披露后续进度。"
)
_PDF = b"%PDF-frozen-proof-fixture"


def _scope(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    index = fixtures._index(root)
    item = replace(fixtures._disclosure("1225000001", "控股股东减持计划公告"),
                   url="https://static.cninfo.com.cn/finalpage/2026-10-18/1225000001.PDF")
    _put(index, item, _TEXT)
    index.publish(item.codes, fixtures.NOW)
    with SqliteSession(index.path) as raw:
        raw.run("INSERT OR REPLACE INTO document_tasks VALUES(?,?,?,'downloaded',?,?,?)",
                (item.source, item.identity, item.digest, "documents/original.pdf", hashlib.sha256(_PDF).digest(), len(_PDF)))
    with SqliteSession(index.facts_path) as formal:
        targets = tuple((field.code, field.event_id, field.field)
                        for field in audit.ReadCompanyFieldResults(formal, ("600001",)) if field.state == "verified")
    assert len(targets) == 3
    manifest = audit.FreezeCompanyFieldProofRecovery(index.path, index.facts_path, targets,
                                                     observed_at=fixtures.NOW, sqlite_policy=SqlitePolicyFixture())
    path = tmp_path / "manifest.json"
    AtomicWriteJson(path, manifest)
    return index, path


def _recover(index, path, directory, attempts, execute):
    with RequestsHttpTransport(observe=attempts.append, execute_request=execute) as session:
        return audit.AuditCompanyFieldProofRecovery(
            index.path, index.facts_path, path, directory, observed_at=fixtures.NOW,
            budget_seconds=30, sqlite_policy=SqlitePolicyFixture(), event_policy=fixtures.CompanyEventPolicy(),
            session=session, http_attempts=attempts,
        )


@pytest.mark.parametrize("failure", (None, "pdf", "body", "transport"))
def test_recovery_requires_both_original_hashes_and_never_repeats_consumed_object(tmp_path, monkeypatch, failure):
    index, path = _scope(tmp_path)
    before = index.path.read_bytes(), index.facts_path.read_bytes()
    monkeypatch.setattr(audit.CninfoMarketDisclosureSource, "_parse_document",
                        lambda *_args, **_kwargs: _TEXT if failure != "body" else _TEXT + "changed")
    calls = []
    attempts = []

    def execute(request):
        calls.append(request)
        if failure == "transport":
            raise HttpTimeout("fixture")
        return HttpResponse(200, _PDF if failure != "pdf" else _PDF + b"changed")

    directory = tmp_path / "isolated"
    report = _recover(index, path, directory, attempts, execute)
    result = report["summary"]["field_proof_recovery"]
    assert result["complete"] and result["network_requests"] == result["attempted_objects"] == 1
    assert result["source_unchanged"] and result["source_data_writes"] == 0
    assert result["reproved_fields"] == (3 if failure is None else 0)
    assert result["recovered_objects"] == (1 if failure is None else 0)
    assert report["status"] == ("passed" if failure is None else "degraded")
    assert len(calls) == 1 and calls[0].timeout_seconds <= 15
    restarted = _recover(index, path, directory, [], lambda _: pytest.fail("consumed object fetched twice"))
    assert restarted["summary"]["field_proof_recovery"]["network_requests"] == 1
    assert before == (index.path.read_bytes(), index.facts_path.read_bytes())


def test_unknown_durable_request_intent_is_not_reissued_or_reported_complete(tmp_path, monkeypatch):
    index, path = _scope(tmp_path)
    monkeypatch.setattr(audit.CninfoMarketDisclosureSource, "_parse_document", lambda *_args, **_kwargs: _TEXT)
    directory = tmp_path / "isolated"
    with pytest.raises(InterruptedError, match="unrelated"):
        _recover(index, path, directory, [], lambda _: (_ for _ in ()).throw(InterruptedError("unrelated")))
    result = _recover(index, path, directory, [], lambda _: pytest.fail("unknown request repeated"))
    summary = result["summary"]["field_proof_recovery"]
    assert result["status"] == "degraded" and not summary["complete"]
    assert not summary["network_request_count_complete"] and summary["attempted_objects"] == 1
    assert summary["material_observations"][0]["requests"] is None


def test_changed_frozen_scope_rejects_before_network_or_directory_creation(tmp_path):
    index, path = _scope(tmp_path)
    with SqliteSession(index.path) as raw:
        raw.run("UPDATE disclosures SET url='https://static.cninfo.com.cn/finalpage/changed.PDF'")
    directory = tmp_path / "isolated"
    with pytest.raises(ValueError, match="manifest_identity_changed"):
        _recover(index, path, directory, [], lambda _: pytest.fail("changed source fetched"))
    assert not directory.exists()


def test_preparation_text_budget_expiry_stops_before_first_http_and_never_passes(tmp_path, monkeypatch):
    index, path = _scope(tmp_path)
    CompanyDocumentFilesFixture(index.path.parent / "text").write(_TEXT)
    with SqliteSession(index.path) as raw:
        raw.run("UPDATE disclosures SET body_retained=1")
    clock = [0.0]
    monkeypatch.setattr(audit.time, "monotonic", lambda: clock[0])

    def expired_read(self, digest, *, check=None):
        clock[0] = 31
        check()
        pytest.fail("preparation continued after its text budget expired")

    monkeypatch.setattr(audit.CompanyDocumentFiles, "read", expired_read)
    directory = tmp_path / "isolated"
    with pytest.raises(TimeoutError, match="recovery_deadline_exceeded"):
        _recover(index, path, directory, [], lambda _: pytest.fail("HTTP after preparation expiry"))
    assert not (directory / "proof-recovery.json").exists()


def test_same_recovery_directory_rejects_concurrent_owner_before_http(tmp_path):
    index, path = _scope(tmp_path)
    directory = tmp_path / "isolated"
    lock_path = directory.with_name(f".{directory.name}-proof-recovery.lock")
    with ProcessLock(lock_path, label="company-field-proof-recovery"):
        with pytest.raises(ProcessLockError, match="already running"):
            _recover(index, path, directory, [], lambda _: pytest.fail("concurrent HTTP"))
    assert not directory.exists()


@pytest.mark.parametrize("link", ("hardlink", "symlink"))
def test_resume_rejects_rebound_database_before_any_source_write_or_http(tmp_path, monkeypatch, link):
    index, path = _scope(tmp_path)
    monkeypatch.setattr(audit.CninfoMarketDisclosureSource, "_parse_document", lambda *_args, **_kwargs: _TEXT)
    directory = tmp_path / "isolated"
    _recover(index, path, directory, [], lambda _: HttpResponse(200, _PDF))
    before = index.path.read_bytes(), index.facts_path.read_bytes()
    clone = directory / "formal" / "company_facts.sqlite3"
    clone.unlink()
    if link == "hardlink":
        os.link(index.facts_path, clone)
    else:
        clone.symlink_to(index.facts_path)
    with pytest.raises(ValueError, match="output_(independent_file_required|symlink_rejected)"):
        _recover(index, path, directory, [], lambda _: pytest.fail("rebound output fetched"))
    assert before == (index.path.read_bytes(), index.facts_path.read_bytes())
