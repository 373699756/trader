"""Failed-PDF diagnostics are bounded, read-only and expose only aggregate request metadata."""

from dataclasses import replace

import pytest

from scripts.diagnose_runtime import DiagnosticResult, _parser, _validate, build_commands, build_report
from scripts.runtime_diagnostics import company_document_download_audit as audit
from tests.component import test_company_evidence as fixtures
from tests.helpers.configuration import SqlitePolicyFixture
from trader.download.domain.company_evidence import DisclosurePage
from trader.infra.http_contracts import HttpResponse, HttpTimeout
from trader.infra.http_transport import RequestsHttpTransport


def test_failed_pdf_probe_compares_deadlines_without_active_writes(tmp_path, monkeypatch):
    index = fixtures._index(tmp_path)
    index.prepare_publication(("600001",), fixtures.NOW)
    partition = index.partitions(("sse",), fixtures.NOW)[0]
    item = replace(fixtures._disclosure(title="收到立案告知书"), url="https://static.cninfo.com.cn/finalpage/fixture.PDF")
    index.commit_page(partition, DisclosurePage((item,), 1, False), fixtures.NOW)
    index.document_failure(item, "test-text", "timeout", fixtures.NOW, fixtures.NOW)
    calls = []
    clock = [0.0]

    def execute(request):
        calls.append(request)
        if request.timeout_seconds == 8:
            raise HttpTimeout("fixture")
        return HttpResponse(200, b"%PDF-test")

    monkeypatch.setattr(audit, "OpenExternalHttp", lambda **kwargs: RequestsHttpTransport(
        monotonic=lambda: clock[0], execute_request=execute, **kwargs,
    ))
    monkeypatch.setattr(audit.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(audit.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    before = index.path.read_bytes()
    result = audit.AuditCompanyDocumentDownloads(index.path, budget_seconds=150, sqlite_policy=SqlitePolicyFixture())
    assert result["status"] == "passed"
    summary = result["summary"]["document_download_audit"]
    assert summary["complete"] and summary["production_passed"] and summary["read_only"]
    assert len(calls) == 5 and calls[-1].timeout_seconds == 120 and calls[-1].io_timeout_seconds == 8
    assert calls[-1].max_response_bytes == 64 * 1024 * 1024
    assert not summary["observations"][0]["passed"] and summary["observations"][-1]["passed"]
    assert index.path.read_bytes() == before


def test_probe_without_failed_documents_does_not_contact_source(tmp_path, monkeypatch):
    index = fixtures._index(tmp_path)
    monkeypatch.setattr(audit, "OpenExternalHttp", lambda **_: pytest.fail("unexpected source access"))
    result = audit.AuditCompanyDocumentDownloads(index.path, budget_seconds=150, sqlite_policy=SqlitePolicyFixture())
    assert result["status"] == "degraded"
    assert result["summary"]["document_download_audit"]["sample_size"] == 0


def test_document_probe_routes_without_replay_directory_and_filters_report_fields():
    options, _ = _validate(_parser().parse_args([
        "--profile", "company-resume", "--company-document-audit",
    ]))
    assert "--document-only" in build_commands(options)[0].argv
    with pytest.raises(ValueError, match="mutually exclusive"):
        _validate(_parser().parse_args([
            "--profile", "company-resume", "--company-document-audit", "--company-storage-audit",
        ]))
    summary = {"document_download_audit": {
        "read_only": True, "url": "secret", "observations": [{
            "disclosure_id": 1, "url": "secret", "body": "private", "attempts": [{"seconds": 1, "raw": "private"}],
        }],
    }}
    result = build_report("company-resume", [DiagnosticResult(
        "company_resume", 0, 1, {"schema_version": "company-resume", "status": "passed", "summary": summary}, None,
    )])
    assert "secret" not in str(result) and "private" not in str(result)
