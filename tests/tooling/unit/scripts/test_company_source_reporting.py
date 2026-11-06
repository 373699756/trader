"""Company source probes preserve bounded observations without qualifying field values."""

from __future__ import annotations

import json

import pytest

from scripts.core.contracts import DiagnosticResult
from scripts.core.projections import BuildReport, NormalizeActionResult


def _annual(**changes) -> dict[str, object]:
    return {
        "schema_version": "company-annual-income-source", "status": "decoded", "read_only": True,
        "source": "eastmoney_f10", "code": "600001", "requested_periods": ["2025-12-31"],
        "field_correctness_verified": False, "full_window_coverage_verified": False,
        "records": [{"report_date": "2025-12-31", "operating_revenue": 123.0, "currency": "CNY"}],
        **changes,
    }


def _structured(**changes) -> dict[str, object]:
    return {
        "schema_version": "company-structured-field-sampling", "status": "passed", "read_only": True,
        "production_adopted": False, "field_correctness_verified": False, "full_window_coverage_verified": False,
        "sources": [{"source": "eastmoney", "dataset": "contract", "status": "sampled", "rows": 1}],
        **changes,
    }


@pytest.mark.parametrize("source", (
    {"schema_version": "research-source-compare", "status": "passed"},
    _annual(),
    _structured(),
))
@pytest.mark.parametrize("value,expected", (("a" * 64, "a" * 64), ("PRIVATE_RUNTIME_CONFIGURATION", None)))
def test_source_reports_keep_only_the_selected_configuration_hash(source, value, expected) -> None:
    payload = dict(source, runtime_configuration_sha256=value)
    report = BuildReport("research-source-compare", (DiagnosticResult("research_source_compare", 0, 1.0, payload, None),))
    assert report["checks"][0]["summary"]["runtime_configuration_sha256"] == expected
    assert "PRIVATE_RUNTIME_CONFIGURATION" not in json.dumps(report)


@pytest.mark.parametrize("source", (_annual(), _annual(status="degraded"), _structured(), _structured(status="degraded")))
def test_explicit_source_observations_are_degraded_and_keep_native_identity(source) -> None:
    result = DiagnosticResult("research_source_compare", 0, 1.0, source, None)
    normalized = NormalizeActionResult("research-source-compare", result)
    assert NormalizeActionResult("research-source-compare", normalized) == normalized
    report = BuildReport("research-source-compare", (normalized,))
    check = report["checks"][0]
    assert report["status"] == "degraded"
    assert check["schema_version"] == source["schema_version"]
    assert check["summary"]["observation_status"] == source["status"]
    assert check["summary"]["field_correctness_verified"] is False
    assert check["summary"]["full_window_coverage_verified"] is False
    if source["schema_version"] == "company-annual-income-source":
        assert check["summary"]["records"][0]["operating_revenue"] == 123.0
    else:
        assert check["summary"]["sources"][0]["dataset"] == "contract"
        assert check["summary"]["production_adopted"] is False


@pytest.mark.parametrize("source", (
    _annual(field_correctness_verified=True),
    _annual(full_window_coverage_verified=True),
    _annual(read_only=False),
    _structured(production_adopted=True),
    _structured(field_correctness_verified=True),
    _structured(sources={}),
))
def test_invalid_acceptance_claims_fail_closed_without_publishing_true_quality_flags(source) -> None:
    result = DiagnosticResult("research_source_compare", 0, 1.0, source, None)
    report = BuildReport("research-source-compare", (result,))
    assert report["status"] == "failed"
    assert report["checks"][0]["summary"]["field_correctness_verified"] is False
    assert report["checks"][0]["summary"]["full_window_coverage_verified"] is False


def test_annual_projection_bounds_records_and_never_forwards_provider_rows() -> None:
    record = {
        "report_date": "2025-12-31", "disclosed_at": "x" * 1000, "updated_at": "y" * 1000,
        "currency": "CNY", "record_hash": "a" * 64, "operating_revenue": float("inf"),
        "TOTAL_OPERATE_INCOME": "PRIVATE_PROVIDER_PAYLOAD", "raw": "PRIVATE_PROVIDER_PAYLOAD",
    }
    source = _annual(records=[record] * 20, raw="PRIVATE_PROVIDER_PAYLOAD")
    report = BuildReport("research-source-compare", (DiagnosticResult("research_source_compare", 0, 1.0, source, None),))
    summary = report["checks"][0]["summary"]
    assert len(summary["records"]) == 8 and summary["records_truncated"]
    assert len(summary["records"][0]["disclosed_at"]) == 64
    assert summary["records"][0]["operating_revenue"] is None
    assert "PRIVATE_PROVIDER_PAYLOAD" not in json.dumps(report)


def test_structured_projection_bounds_schema_fields_and_explicit_source_identities() -> None:
    identity = {"SECURITYCODE": "x" * 1000, "AMOUNTS": float("inf"), "YEAR": True, "RAW": "PRIVATE_PROVIDER_PAYLOAD"}
    sample = {
        "source": "eastmoney", "dataset": "contract", "report": "RPTA_WEB_ZDHT_LIST", "status": "sampled",
        "returned_fields": ["VALID_FIELD"] * 300 + ["private-provider-value"],
        "fields": [{"field": "AMOUNTS", "present_rows": 1, "private": "PRIVATE_PROVIDER_PAYLOAD"}] * 40,
        "identity_samples": [identity] * 12, "raw": "PRIVATE_PROVIDER_PAYLOAD",
    }
    source = _structured(sources=[sample] * 9, raw="PRIVATE_PROVIDER_PAYLOAD")
    report = BuildReport("research-source-compare", (DiagnosticResult("research_source_compare", 0, 1.0, source, None),))
    summary = report["checks"][0]["summary"]
    assert len(summary["sources"]) == 4
    first = summary["sources"][0]
    assert len(first["returned_fields"]) == 256 and len(first["fields"]) == 32
    assert len(first["identity_samples"]) == 5
    assert first["identity_samples"][0] == {"SECURITYCODE": "x" * 160}
    assert "PRIVATE_PROVIDER_PAYLOAD" not in json.dumps(report)


@pytest.mark.parametrize("typed,expected", (
    (
        {"status": "decoded", "observations": 5, "reasons": {"event_identity_unverified": 5},
         "production_adopted": True, "field_correctness_verified": True},
        {"status": "decoded", "failure": None, "observations": 5,
         "reasons": {"event_identity_unverified": 5}, "production_adopted": False, "field_correctness_verified": False},
    ),
    (
        {"status": "rejected", "failure": "ValueError", "production_adopted": False},
        {"status": "rejected", "failure": "ValueError", "observations": None,
         "reasons": {}, "production_adopted": False, "field_correctness_verified": False},
    ),
))
def test_structured_projection_preserves_type_decoding_and_identity_rejection(typed, expected) -> None:
    sample = {"source": "eastmoney", "dataset": "contract", "status": "sampled", "rows": 5, "typed_fields": typed}
    report = BuildReport("research-source-compare", (
        DiagnosticResult("research_source_compare", 0, 1.0, _structured(sources=[sample]), None),
    ))
    summary = report["checks"][0]["summary"]
    assert report["status"] == "degraded"
    assert summary["sources"][0]["typed_fields"] == expected
    assert summary["production_adopted"] is False and summary["field_correctness_verified"] is False


@pytest.mark.parametrize("count", (True, -1, 501, float("nan"), "PRIVATE_PROVIDER_PAYLOAD"))
def test_structured_type_projection_bounds_counts_and_rejects_unselected_payloads(count) -> None:
    typed = {
        "status": "PRIVATE_PROVIDER_PAYLOAD", "failure": "PRIVATE_PROVIDER_PAYLOAD", "observations": count,
        "reasons": {"event_identity_unverified": count, "point_in_time_unverified": 500,
                    "source_after_cutoff": 1, "PRIVATE_PROVIDER_PAYLOAD": 1},
        "raw": "PRIVATE_PROVIDER_PAYLOAD", "observations_payload": [{"raw": "PRIVATE_PROVIDER_PAYLOAD"}] * 1000,
    }
    sample = {"source": "eastmoney", "dataset": "contract", "status": "sampled", "typed_fields": typed}
    report = BuildReport("research-source-compare", (
        DiagnosticResult("research_source_compare", 0, 1.0, _structured(sources=[sample]), None),
    ))
    result = report["checks"][0]["summary"]["sources"][0]["typed_fields"]
    assert result["status"] is None and result["failure"] is None and result["observations"] is None
    assert result["reasons"] == {"point_in_time_unverified": 500, "source_after_cutoff": 1}
    assert result["production_adopted"] is False and result["field_correctness_verified"] is False
    assert "PRIVATE_PROVIDER_PAYLOAD" not in json.dumps(report)
