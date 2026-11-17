"""The candidate exposes the actual bounded document scope and its admission limits."""

import json

from scripts.core.contracts import DiagnosticResult
from scripts.core.projections import BuildReport


def test_pipeline_public_projection_retains_scope_and_drops_private_payloads():
    payload = { "status": "degraded", "findings": ["sample_incomplete"],
               "summary": {"pipeline_audit": {
                   "isolated_sample": True, "source_admission_verified": False, "production_enabled": False,
                   "performance_accepted": False, "full_sync_verified": False,
                   "equivalent_documents": False, "equivalent_facts": False,
                   "runs": [{"workers": 1, "pacing": "shared", "sample_documents": 2, "extracted_documents": 1,
                             "seconds": 0.1, "failures": ["timeout"], "restart_unchanged": True,
                             "facts_digest": "a" * 64, "documents_digest": "b" * 64,
                             "directory": {"complete": False, "pages": 1, "rows": 30, "private_payload": "secret"},
                             "operations": [{"operation": "facts_publish", "calls": 1, "private_payload": "secret",
                                             "failures": [{"category": "timeout", "calls": 1, "private_payload": "secret"}]}],
                             "private_payload": "secret"}],
               }}}
    report = BuildReport("company-resume", (DiagnosticResult("company_resume", 0, 0.1, payload, None),))
    public = report["checks"][0]["summary"]["pipeline_audit"]
    assert public["source_admission_verified"] is False and public["performance_accepted"] is False
    assert public["runs"][0]["restart_unchanged"] is True
    assert public["runs"][0]["operations"][0]["operation"] == "facts_publish"
    assert "secret" not in json.dumps(report) and "publication_interval_seconds" not in json.dumps(report)
