"""Crash boundaries and physical requests, independent of terminal counters."""

import hashlib
import os
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from tests.helpers.download.company import company_evidence as fixtures
from tests.helpers.shared.configuration import CninfoMarketDisclosureSourceFixture
from tests.helpers.download.company.company_document_parser import _pdf
from trader.download.domain.company_evidence import BuildCompanyDocumentPolicies, CompanyDocumentFailure, DisclosurePage
from trader.download.infra.company_raw_files import CompanyRawFiles
from trader.infra.http_contracts import HttpResponse, HttpTimeout
from trader.infra.http_transport import RequestsHttpTransport
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction


def _raw(root):
    return CompanyRawFiles(root, capacity=2 * 1024**3, analysis_reserve=512 * 1024**2, free_reserve=1)


def _item():
    return replace(fixtures._disclosure("1000000001", "关于收到立案告知书的公告"),
                   url="https://static.cninfo.com.cn/finalpage/fixture.pdf")


def test_usage_tolerates_scratch_directory_removed_during_scan(tmp_path, monkeypatch):
    scratch = tmp_path / "documents" / "text-scratch"
    scratch.mkdir(parents=True)
    retained = tmp_path / "failed" / "kept.pdf"
    retained.parent.mkdir()
    retained.write_bytes(b"retained-input")
    original = os.scandir

    def scandir(path):
        if Path(path) == scratch and scratch.exists():
            scratch.rmdir()
        return original(path)

    monkeypatch.setattr(os, "scandir", scandir)
    assert _raw(tmp_path).usage() == len(b"retained-input")


def test_usage_does_not_hide_permission_errors_and_underestimate_capacity(tmp_path, monkeypatch):
    def scandir(_path):
        raise PermissionError("fixture directory not readable")

    monkeypatch.setattr(os, "scandir", scandir)
    with pytest.raises(PermissionError, match="not readable"):
        _raw(tmp_path).usage()


def test_restart_reuses_pdf_after_analysis_failure_and_then_reuses_result(tmp_path):
    requests = []
    payload = _pdf()

    def send(request):
        requests.append(request)
        return HttpResponse(200, payload, (("Content-Length", str(len(payload))), ("ETag", '"one"')))

    raw = _raw(tmp_path)
    with RequestsHttpTransport(execute_request=send, monotonic=lambda: 0) as transport:
        source = CninfoMarketDisclosureSourceFixture(transport, raw_files=raw, monotonic=lambda: 0, sleeper=lambda _: None)
        item = _item()
        source.download_document(item)
        assert len(requests) == 1
        restarted = CninfoMarketDisclosureSourceFixture(transport, raw_files=_raw(tmp_path), monotonic=lambda: 0, sleeper=lambda _: None)
        restarted.download_document(item)
        result = restarted.extract_document(item)
        assert result.payload_hash == hashlib.sha256(payload).digest()
        assert restarted.extract_document(item) == result
        assert len(requests) == 1
        assert not tuple(tmp_path.rglob("text-*"))


def test_runtime_configuration_changes_preserve_successful_result(tmp_path):
    original = fixtures._TEST_COMPANY_CONFIGURATION
    tuned = replace(original, document_workers=4, text_workers=3, document_batch_size=80,
                    document_ocr_timeout_seconds=500, document_ocr_page_timeout_seconds=100,
                    document_parse_timeout_seconds=20, publication_interval_seconds=10)
    before = BuildCompanyDocumentPolicies(original, "ocr-fixture", text_identity="text-fixture")
    after = BuildCompanyDocumentPolicies(tuned, "ocr-fixture", text_identity="text-fixture")
    raw = _raw(tmp_path)
    item = _item()
    raw.directory(item).mkdir(parents=True)
    for policy in (before.standard, before.ocr):
        extraction = replace(fixtures._extraction("本公司收到证监会立案告知书。"), policy_id=policy)
        raw.save_result(item, extraction)
        assert raw.result(item, (after.standard, after.ocr, after.limits)) == extraction
    assert before.ocr != after.ocr  # Failed tasks receive a new deadline budget.
    from trader.download.domain.company_evidence import CompanyDocumentOutputIdentity

    old = before.ocr.replace(":retry_layout=orient_rotate_dpi_each_page", "")
    assert CompanyDocumentOutputIdentity(before.ocr) == CompanyDocumentOutputIdentity(old)
    old_text = before.standard.replace(":retry_inputs=1", "")
    assert CompanyDocumentOutputIdentity(before.standard) == CompanyDocumentOutputIdentity(old_text)
    extraction = replace(fixtures._extraction("本公司收到证监会立案告知书。"), policy_id=old_text)
    raw.save_result(item, extraction)
    assert raw.result(item, (after.standard, after.ocr, after.limits)) == extraction


def test_failed_input_survives_restart_success_cleanup_and_obsolete_revision(tmp_path):
    raw = _raw(tmp_path)
    item = _item()
    directory = raw.directory(item)
    directory.mkdir(parents=True)
    payload = _pdf()
    pdf = directory / (hashlib.sha256(payload).hexdigest() + ".pdf")
    pdf.write_bytes(payload)
    (directory / "page-checkpoint.json").write_text("{}")
    retained = raw.retain_failure(item, "disclosure_document_ocr_low_quality", "ocr-policy", fixtures.NOW.isoformat())
    assert "failed" in retained.parts and not directory.exists()
    restarted = _raw(tmp_path)
    assert restarted.local(item).path.read_bytes() == payload
    assert restarted.usage() >= len(payload)
    assert restarted.release(item)
    assert restarted.local(item) is not None
    assert (retained / "page-checkpoint.json").exists()
    assert len(tuple(retained.glob("failure-*.json"))) == 1
    new = replace(item, digest="new-version")
    restarted.directory(new).mkdir(parents=True)
    assert restarted.release(new)
    assert retained.exists()
    constrained = CompanyRawFiles(tmp_path, capacity=100, analysis_reserve=10, free_reserve=1)
    with pytest.raises(RuntimeError, match="company_raw_capacity_exhausted"):
        with constrained.reserve(retained, 1):
            pass
    assert restarted.local(item) is not None


def test_failure_workload_persists_after_restart_and_clears_after_success(tmp_path):
    index = fixtures._index(tmp_path)
    now = fixtures.NOW
    item = _item()
    partition = index.partitions(("sse",), now)[0]
    index.commit_page(partition, DisclosurePage((item,), 1, False), now)
    index.document_failure(item, fixtures._TEST_DOCUMENT_POLICIES.ocr, "disclosure_document_ocr_low_quality", now, now + timedelta(days=1))
    assert index.document_workload(item.codes, now, fixtures._TEST_DOCUMENT_POLICIES).unresolved_failures == 1
    index.prepare_publication(item.codes, now)
    assert index.document_workload(item.codes, now, fixtures._TEST_DOCUMENT_POLICIES).unresolved_failures == 1
    index.commit_document(item, fixtures._extraction("本公司收到中国证监会立案告知书。"), now)
    assert index.document_workload(item.codes, now, fixtures._TEST_DOCUMENT_POLICIES).unresolved_failures == 0


def test_same_content_revision_reuses_analysis_and_survives_other_task_cleanup(tmp_path):
    raw = _raw(tmp_path)
    old, new = _item(), replace(_item(), digest="revised-metadata")
    with RequestsHttpTransport(execute_request=lambda _: HttpResponse(200, _pdf()), monotonic=lambda: 0) as transport:
        source = CninfoMarketDisclosureSourceFixture(transport, raw_files=raw, monotonic=lambda: 0, sleeper=lambda _: None)
        source.download_document(old)
        result = source.extract_document(old)
        source.download_document(new)
        assert raw.local(old).path.samefile(raw.local(new).path)
        assert source.extract_document(new) == result
        assert raw.release(old)
        assert raw.local(new) is not None and source.extract_document(new) == result


@pytest.mark.parametrize("changes", (
    {"ocr_workers": 2}, {"document_queue_limit": 20}, {"document_ocr_page_timeout_seconds": 601},
    {"document_ocr_page_timeout_seconds": 0}, {"document_workers": 0},
))
def test_invalid_pipeline_resource_configuration_is_rejected(changes):
    with pytest.raises(ValueError, match="company_batch_configuration_invalid"):
        replace(fixtures._TEST_COMPANY_CONFIGURATION, **changes)


def test_discovery_frontier_keeps_gap_and_terminal_unknown_closes_processing_only(tmp_path):
    index = fixtures._index(tmp_path)
    now = fixtures.NOW
    start = now.date() - timedelta(days=365)
    partition = next(p for p in index.partitions(("sse",), now) if p.start == now.date())
    item = _item()
    index.commit_page(partition, DisclosurePage((item,), 1, False), now)
    index.prepare_publication(item.codes, now)
    assert index.document_workload(item.codes, now, fixtures._TEST_DOCUMENT_POLICIES).watermarks[0].discovered_day is None
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        connection.run("INSERT INTO coverage VALUES('sse',?,?,?)", (str(start), str(now.date() - timedelta(days=1)), now.isoformat()))
    state = index.document_workload(item.codes, now, fixtures._TEST_DOCUMENT_POLICIES)
    assert state.watermarks[0].discovered_day == now.date()
    assert state.watermarks[0].processed_day == now.date() - timedelta(days=1)
    index.document_failure(item, fixtures._TEST_DOCUMENT_POLICIES.ocr, "disclosure_document_ocr_low_quality", now, now + timedelta(days=1))
    assert index.document_workload(item.codes, now, fixtures._TEST_DOCUMENT_POLICIES).watermarks[0].processed_day < now.date()
    index.publish(item.codes, now)
    state = index.document_workload(item.codes, now, fixtures._TEST_DOCUMENT_POLICIES)
    assert state.watermarks[0].processed_day == state.watermarks[0].discovered_day == now.date()
    assert state.unextractable == 1 and state.due == 0
    with SqliteSession(index.facts_path) as connection:
        assert connection.run("SELECT announcement_state FROM company_facts").require_one()[0] != 2


def test_dispatch_uses_only_reference_eligible_population_without_changing_directory_counts(tmp_path):
    index = fixtures._index(tmp_path)
    now = fixtures.NOW
    part = next(p for p in index.partitions(("sse",), now) if p.start == now.date())
    shared = replace(_item(), codes=("600001", "600002"))
    excluded = replace(shared, identity="excluded", codes=("600002",), digest="excluded")
    index.commit_page(part, DisclosurePage((shared, excluded), 2, False), now)
    index.prepare_publication(("600001",), now)
    tasks = index.pending_documents(("600001",), now, policies=fixtures._TEST_DOCUMENT_POLICIES)
    assert len(tasks) == 1 and tasks[0].disclosure.codes == ("600001",)
    assert not index.document_required(excluded)
    with SqliteSession(index.path) as connection:
        assert connection.run("SELECT COUNT(*) FROM disclosures").require_one()[0] == 2


def test_missing_formal_database_is_not_silently_recreated(tmp_path):
    index = fixtures._index(tmp_path)
    index.facts_path.unlink()
    with pytest.raises(ValueError, match="company_database_pair_incomplete"):
        index.initialize()
    assert not index.facts_path.exists()


def test_new_verified_replacement_skips_old_task_but_new_independent_event_does_not(tmp_path):
    index = fixtures._index(tmp_path)
    now = fixtures.NOW
    part = next(p for p in index.partitions(("sse",), now) if p.start == now.date())
    old = replace(_item(), identity="1000000002", published_at=now - timedelta(minutes=2), digest="older")
    independent = replace(old, identity="1000000003", digest="independent")
    new = replace(_item(), identity="1000000004", title="关于收到立案告知书的更正公告", digest="newer")
    index.commit_page(part, DisclosurePage((old, independent, new), 3, False), now)
    index.prepare_publication(old.codes, now)
    index.commit_document(new, fixtures._extraction("本公司未被立案。本公告完整替代原公告编号1000000002。"), now)
    index.publish(old.codes, now)
    pending = index.pending_documents(old.codes, now, policies=fixtures._TEST_DOCUMENT_POLICIES)
    assert {task.disclosure.identity for task in pending} == {independent.identity}
    assert not index.document_required(old) and index.document_required(independent)
    with SqliteSession(index.path) as connection:
        assert connection.run("SELECT reason FROM document_supersessions").require_one()[0] == "official_complete_replacement"
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        connection.run("UPDATE document_supersessions SET extraction_hash=?", (b"x" * 32,))
        connection.run("UPDATE document_publications SET extraction_hash=?", (b"x" * 32,))
    pending = index.pending_documents(old.codes, now, policies=fixtures._TEST_DOCUMENT_POLICIES)
    assert {task.disclosure.identity for task in pending} == {old.identity, independent.identity}


def test_partial_new_financial_report_keeps_complete_dated_report_without_mixing_fields():
    from trader.download.domain.company_evidence import CompanyEvidenceInput, CompanyFinancial
    from trader.download.domain.company_facts import CompileCompanyFacts
    from trader.download.domain.company_events import CompanyDataState

    now = fixtures.NOW
    old = CompanyFinancial("600001", now.date() - timedelta(days=180), now - timedelta(days=150), *([1.0] * 8))
    source = CompanyEvidenceInput("600001", now, now.date() - timedelta(days=365), now.date(), "test", (), True,
                                  (old,), None, (), False, (), False, ())
    previous = CompileCompanyFacts(source, fixtures.CompanyEventPolicy())
    partial = replace(old, report_date=now.date() - timedelta(days=30), published_at=now, basic_eps=None, book_value_per_share=9.0)
    current = CompileCompanyFacts(replace(source, financials=(partial,)), fixtures.CompanyEventPolicy(), previous=previous)
    assert current.financial == old and current.financial_state is CompanyDataState.PARTIAL
    complete = replace(partial, basic_eps=2.0)
    latest = CompileCompanyFacts(replace(source, financials=(complete,)), fixtures.CompanyEventPolicy(), previous=current)
    assert latest.financial == complete and latest.financial_state is CompanyDataState.COMPLETE


def test_incomplete_download_uses_validated_range_without_restarting_bytes(tmp_path):
    payload = _pdf()
    offsets = []

    class Transport:
        def execute(self, request):
            fields = dict(request.headers)
            offset = int(fields["Range"].split("=")[1].split("-")[0]) if "Range" in fields else 0
            offsets.append(offset)
            headers = (("ETag", '"one"'), ("Content-Length", str(len(payload) - offset)))
            if offset:
                headers += (("Content-Range", f"bytes {offset}-{len(payload) - 1}/{len(payload)}"),)
            request.receive_headers(206 if offset else 200, headers)
            request.receive_chunk(payload[offset:len(payload)//2] if not offset else payload[offset:])
            if not offset:
                raise HttpTimeout("http_timeout")
            return HttpResponse(206, b"", headers)

    source = CninfoMarketDisclosureSourceFixture(Transport(), raw_files=_raw(tmp_path), monotonic=lambda: 0, sleeper=lambda _: None)
    source.download_document(_item())
    assert offsets == [0, len(payload)//2]
    assert source._raw_files.local(_item()).payload_hash == hashlib.sha256(payload).digest()


def test_formal_commit_survives_crash_before_collection_receipt(tmp_path, monkeypatch):
    index = fixtures._index(tmp_path)
    partition = index.partitions(("sse",), fixtures.NOW)[0]
    item = _item()
    index.commit_page(partition, DisclosurePage((item,), 1, False), fixtures.NOW)
    index.commit_document(item, fixtures._extraction("本公司收到中国证监会立案告知书。"), fixtures.NOW)
    publish = index._facts.publish

    def crash(*args, **kwargs):
        publish(*args, **kwargs)
        raise RuntimeError("simulated_process_loss")

    monkeypatch.setattr(index._facts, "publish", crash)
    with pytest.raises(RuntimeError, match="simulated_process_loss"):
        index.publish(("600001",), fixtures.NOW)
    with SqliteSession(index.facts_path) as connection:
        assert connection.run("SELECT count(*) FROM company_input_receipt").require_one()[0] == 1
    with SqliteSession(index.path) as connection:
        assert connection.run("SELECT count(*) FROM document_publications").require_one()[0] == 0
    monkeypatch.setattr(index._facts, "publish", publish)
    index.prepare_publication(("600001",), fixtures.NOW)
    assert index.pending_documents(("600001",), fixtures.NOW, policies=fixtures._TEST_DOCUMENT_POLICIES) == ()


def test_ocr_pages_resume_after_timeout_without_repeating_successful_pages(tmp_path, monkeypatch):
    from trader.download.infra.company_document_ocr import CompanyOcrCapability, CompanyOcrEngine

    path = tmp_path / "document.pdf"
    path.write_bytes(_pdf(32))
    digest = hashlib.sha256(path.read_bytes()).digest()
    pages = []
    current = [0]
    fail = [True]
    engine = CompanyOcrEngine(CompanyOcrCapability("fixture", "tesseract", "pdftoppm", "eng"),
                             timeout_seconds=600, page_timeout_seconds=120, max_pages=50, dpi=150)

    def run(command, *args, **kwargs):
        if command[0] == "pdfinfo":
            return ("Pages: 32\n" + "".join(f"Page {p} size: 200 x 200 pts\n" for p in range(1, 33))).encode()
        current[0] = int(command[command.index("-f") + 1])
        return b"rendered"

    def recognize(*args):
        pages.append(current[0])
        if current[0] == 22 and fail[0]:
            raise CompanyDocumentFailure("disclosure_document_ocr_timeout")
        return f"page {current[0]} " + "verified " * 12, [90.0]

    monkeypatch.setattr(engine, "_run", run)
    monkeypatch.setattr(engine, "_recognize", recognize)
    with pytest.raises(CompanyDocumentFailure, match="ocr_timeout"):
        engine.extract(path, digest)
    assert len(tuple(tmp_path.rglob("*.json"))) == 21
    fail[0] = False
    result, quality = engine.extract(path, digest)
    assert quality == 90 and "page 32" in result
    assert pages == [*range(1, 23), *range(22, 33)]
    assert not tuple(tmp_path.rglob("page.png"))
