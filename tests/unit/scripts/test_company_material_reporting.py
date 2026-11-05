"""Material observations remain bounded diagnostics without quality acceptance."""

from __future__ import annotations

import json

import pytest

from scripts.core.contracts import DiagnosticResult
from scripts.core.projections import BuildReport, NormalizeActionResult


def _material(**changes) -> dict[str, object]:
    return {
        "schema_version": "company-field-material",
        "status": "observed",
        "read_only": True,
        "stable_read": True,
        "field_correctness_verified": False,
        "missing_source_keys": [],
        "items": [],
        **changes,
    }


def test_stable_material_observations_are_degraded_and_normalization_is_idempotent() -> None:
    original = DiagnosticResult("company_resume", 0, 1.0, _material(), None)
    normalized = NormalizeActionResult("company-resume", original)
    assert NormalizeActionResult("company-resume", normalized) == normalized
    report = BuildReport("company-resume", (normalized,))
    assert report["status"] == "degraded"
    summary = report["checks"][0]["summary"]
    assert summary["read_only"] and summary["stable_read"]
    assert summary["field_correctness_verified"] is False
    assert summary["observation_status"] == "observed"


@pytest.mark.parametrize("changes", (
    {"status": "passed"},
    {"field_correctness_verified": True},
    {"read_only": False},
    {"stable_read": False},
    {"items": {}},
))
def test_material_quality_claims_or_invalid_evidence_never_pass(changes) -> None:
    result = DiagnosticResult("company_resume", 0, 1.0, _material(**changes), None)
    report = BuildReport("company-resume", (result,))
    assert report["status"] == "failed"
    assert report["checks"][0]["summary"]["field_correctness_verified"] is False


def test_unstable_reads_keep_the_native_degraded_diagnostic() -> None:
    result = DiagnosticResult("company_resume", 0, 1.0, _material(status="degraded", stable_read=False), None)
    report = BuildReport("company-resume", (result,))
    assert report["status"] == "degraded"
    assert report["checks"][0]["summary"]["stable_read"] is False


def test_material_projection_limits_every_document_and_drops_unselected_fields() -> None:
    excerpt = {"line": 1, "text": "节" * 1000, "body": "PRIVATE_FULL_MATERIAL"}
    contract = {
        "event_id": "e" * 1000, "subject": "issuer", "status": "valid", "contract_amount": 20,
        "annual_revenue": 100, "binding_confirmed": True, "amount_basis": "total",
        "full_material": "PRIVATE_FULL_MATERIAL",
    }
    item = {
        "source_key": "k" * 1000, "code": "600001", "title": "题" * 1000,
        "excerpts": [excerpt] * 25, "contracts": [contract] * 25,
        "source_url": "https://untrusted.invalid/PRIVATE_FULL_MATERIAL",
        "body": "PRIVATE_FULL_MATERIAL",
    }
    source = _material(items=[item] * 40, missing_source_keys=["x" * 1000] * 10, full_material="PRIVATE_FULL_MATERIAL")
    report = BuildReport("company-resume", (DiagnosticResult("company_resume", 0, 1.0, source, None),))
    summary = report["checks"][0]["summary"]
    assert len(summary["items"]) == 32 and summary["items_truncated"]
    assert len(summary["missing_source_keys"]) == 6 and len(summary["missing_source_keys"][0]) == 240
    first = summary["items"][0]
    assert len(first["source_key"]) == 240 and len(first["title"]) == 160
    assert len(first["excerpts"]) == 16 and len(first["excerpts"][0]["text"]) == 150
    assert len(first["contracts"]) == 16 and first["contracts_truncated"]
    assert len(first["contracts"][0]["event_id"]) == 160
    assert first["source_url"] is None
    assert "PRIVATE_FULL_MATERIAL" not in json.dumps(report)
