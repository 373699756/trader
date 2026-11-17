"""Failed-PDF diagnostics are bounded, read-only and expose only aggregate request metadata."""

from tests.helpers.shared.tool_cli import BuildToolCommands, ParseBusinessOptions, ParseToolArguments

from scripts.core.catalog import SelectionArguments


import pytest

from scripts.core.contracts import DiagnosticResult
from scripts.core.projections import BuildReport
from scripts.download.company import company_document_download_audit as audit
from tests.helpers.download.company import company_evidence as fixtures
from tests.helpers.shared.configuration import SqlitePolicyFixture




def test_probe_without_failed_documents_does_not_contact_source(tmp_path, monkeypatch):
    index = fixtures._index(tmp_path)
    monkeypatch.setattr(audit, "OpenExternalHttp", lambda **_: pytest.fail("unexpected source access"))
    result = audit.AuditCompanyDocumentDownloads(index.path, budget_seconds=150, sqlite_policy=SqlitePolicyFixture())
    assert result["status"] == "degraded"
    assert result["summary"]["document_download_audit"]["sample_size"] == 0




def test_document_probe_routes_without_replay_directory_and_filters_report_fields():
    options = ParseBusinessOptions(ParseToolArguments([
        *SelectionArguments('company-resume'), "--company-document-audit",
    ]))
    assert "--document-only" in BuildToolCommands(options)[0].argv
    with pytest.raises(SystemExit) as error:
        ParseToolArguments([
            *SelectionArguments('company-resume'), "--company-document-audit", "--company-database-audit",
        ])
    assert error.value.code == 2
    summary = {"document_download_audit": {
        "read_only": True, "url": "secret", "observations": [{
            "disclosure_id": 1, "url": "secret", "body": "private", "attempts": [{"seconds": 1, "raw": "private"}],
        }],
    }}
    result = BuildReport("company-resume", [DiagnosticResult(
        "company_resume", 0, 1, {"schema_version": "company-resume", "status": "passed", "summary": summary}, None,
    )])
    assert "secret" not in str(result) and "private" not in str(result)




def test_capacity_recovery_rejects_output_overlapping_source_before_writes(tmp_path):
    index = fixtures._index(tmp_path)
    before = index.path.read_bytes()
    with pytest.raises(ValueError, match="company_replay_overlaps"):
        audit.AuditCompanyDocumentDownloads(
            index.path, budget_seconds=30, sqlite_policy=SqlitePolicyFixture(), directory=tmp_path / "recovery",
        )
    assert index.path.read_bytes() == before
