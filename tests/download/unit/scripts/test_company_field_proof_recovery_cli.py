"""Recovery is explicit, bounded and cannot claim production acceptance."""

import json

import pytest

from scripts.core.contracts import DiagnosticResult
from scripts.core.projections import BuildReport
from tests.helpers.shared.tool_cli import BuildToolCommands, ParseBusinessOptions, ParseToolArguments


def _arguments():
    return ["--business", "download", "--module", "company", "--action", "coverage"]


def _report():
    return {
         "status": "degraded", "findings": ["unreproved"],
        "summary": {"field_proof_recovery": {
            "scope": "exact_published_field_proof_materials", "read_only_source": True, "isolated_copy": True,
            "complete": False, "source_data_writes": 0, "network_requests": 2, "network_request_count_complete": True,
            "source_publication_hash": "a" * 64, "field_rule_hash": "b" * 64,
            "planned_objects": 2, "attempted_objects": 2, "exact_text_objects": 1, "recovered_objects": 1,
            "target_fields": 3, "reproved_fields": 1, "unreproved_fields": 2,
            "source_unchanged": True, "budget_exhausted": False,
            "production_adopted": False, "field_correctness_verified": False,
            "full_market_verified": False, "service_http_checked": False,
            "material_observations": [{"source_key": "known", "private_payload": "secret"}],
            "field_results": [], "private_payload": "secret",
        }},
    }


def test_public_recovery_requires_both_explicit_manifest_and_new_directory(tmp_path):
    flag = [*_arguments(), "--company-field-proof-recovery"]
    with pytest.raises(ValueError, match="requires a manifest"):
        ParseBusinessOptions(ParseToolArguments(flag))
    with pytest.raises(ValueError, match="requires --company-field-proof-recovery"):
        ParseBusinessOptions(ParseToolArguments([
            *_arguments(), "--company-field-proof-recovery-manifest", str(tmp_path / "manifest.json"),
        ]))
    options = ParseBusinessOptions(ParseToolArguments([
        *flag, "--company-field-proof-recovery-manifest", str(tmp_path / "manifest.json"),
        "--company-replay-directory", str(tmp_path / "recovery"),
    ]))
    command = BuildToolCommands(options)[0].argv
    assert "--field-proof-recovery-only" in command and "--field-proof-recovery-manifest" in command


def test_recovery_public_projection_excludes_private_material():
    report = BuildReport("company-resume", (DiagnosticResult("company_resume", 0, 0.1, _report(), None),))
    assert report["status"] == "degraded"
    assert "secret" not in json.dumps(report)


@pytest.mark.parametrize("key,value", (
    ("network_requests", True), ("planned_objects", 53), ("recovered_objects", 2),
    ("source_data_writes", 1), ("production_adopted", True), ("unreproved_fields", 0),
))
def test_recovery_rejects_invalid_counts_and_acceptance_claims(key, value):
    report = _report()
    report["summary"]["field_proof_recovery"][key] = value
    public = BuildReport("company-resume", (DiagnosticResult("company_resume", 0, 0.1, report, None),))
    assert public["status"] == "failed"


def test_partial_recovery_cannot_claim_a_complete_pass():
    report = _report()
    report["status"] = "passed"
    report["findings"] = []
    public = BuildReport("company-resume", (DiagnosticResult("company_resume", 0, 0.1, report, None),))
    assert public["status"] == "failed"


def test_complete_recovery_with_unknown_physical_request_count_cannot_pass():
    report = _report()
    report["status"] = "passed"
    report["findings"] = []
    recovery = report["summary"]["field_proof_recovery"]
    recovery.update(complete=True, exact_text_objects=2, recovered_objects=2, reproved_fields=3, unreproved_fields=0)
    accepted = BuildReport("company-resume", (DiagnosticResult("company_resume", 0, 0.1, report, None),))
    assert accepted["status"] == "passed"
    recovery["network_request_count_complete"] = False
    rejected = BuildReport("company-resume", (DiagnosticResult("company_resume", 0, 0.1, report, None),))
    assert rejected["status"] == "failed"
