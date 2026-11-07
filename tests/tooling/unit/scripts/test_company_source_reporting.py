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


def test_financial_revision_projection_preserves_bounded_evidence_and_drops_unapproved_fields() -> None:
    source = {
        "status": "degraded", "runtime_configuration_sha256": "a" * 64,
        "raw": "PRIVATE_PROVIDER_PAYLOAD",
        "financial_revision_probe": {
            "source": "eastmoney", "report": "RPT_F10_FINANCE_MAINFINADATA", "code": "600001",
            "report_date": "2026-06-30", "observed_at": "2026-11-07T12:00:00+08:00",
            "read_only": True, "support_status": "sample_verified", "update_filter_verified": True,
            "production_adopted": True, "field_correctness_verified": True, "sort_verified": True,
            "request_count": 3, "elapsed_seconds": 1.5, "failure": "company_financial_revision_private_provider_payload",
            "record": {
                "identity": "600001.SH", "report_date": "2026-06-30", "notice_at": "2026-08-20T00:00:00+08:00",
                "updated_at": "2026-10-30T10:00:00+08:00",
                "values": {"eps": 1.25, "core_profit": float("inf"), "raw": "PRIVATE_PROVIDER_PAYLOAD"},
                "missing_value_fields": ["bps", "PRIVATE_PROVIDER_PAYLOAD"], "raw": "PRIVATE_PROVIDER_PAYLOAD",
            },
            "queries": [{
                "query": "exclude_update_boundary", "status": "empty", "response_sha256": "b" * 64,
                "received_at": "2026-11-07T12:00:00+08:00", "reported_count": 0, "reported_pages": 0,
                "row_count": 0, "response_bytes": 123, "http_status": 200,
                "missing_fields": ["UPDATE_DATE", "PRIVATE_PROVIDER_PAYLOAD"], "raw": "PRIVATE_PROVIDER_PAYLOAD",
            }] * 5,
        },
    }
    report = BuildReport("research-source-compare", (DiagnosticResult("research_source_compare", 0, 1.5, source, None),))
    check = report["checks"][0]
    probe = check["financial_revision_probe"]
    assert report["status"] == "failed" and "schema_version" not in source
    assert check["summary"]["runtime_configuration_sha256"] == "a" * 64
    assert probe["record"]["notice_at"].startswith("2026-08-20")
    assert probe["record"]["updated_at"].startswith("2026-10-30")
    assert len(probe["record"]["values"]) == 8 and probe["record"]["values"]["eps"] == 1.25
    assert probe["record"]["values"]["core_profit"] is None
    assert probe["record"]["missing_value_fields"] == ["bps"]
    assert probe["failure"] is None and probe["update_filter_verified"]
    assert not any(probe[name] for name in ("production_adopted", "field_correctness_verified", "full_window_coverage_verified", "sort_verified"))
    assert probe["request_limit"] == probe["request_count"] == len(probe["queries"]) == 3
    assert probe["max_response_bytes"] == 1024 * 1024
    assert probe["queries"][0]["response_sha256"] == "b" * 64
    assert probe["queries"][0]["missing_fields"] == ["UPDATE_DATE"]
    assert "PRIVATE_PROVIDER_PAYLOAD" not in json.dumps(report) and "private_provider_payload" not in json.dumps(report)


def _revision_probe():
    return {
        "source": "eastmoney", "report": "RPT_F10_FINANCE_MAINFINADATA", "code": "600001",
        "report_date": "2026-06-30", "read_only": True, "production_adopted": False,
        "field_correctness_verified": False, "full_window_coverage_verified": False, "sort_verified": False,
        "support_status": "sample_verified", "update_filter_verified": True,
        "request_count": 3, "request_limit": 3, "max_response_bytes": 1024 * 1024,
        "queries": [
            {"query": "baseline", "status": "decoded", "row_count": 1},
            {"query": "include_update_boundary", "status": "decoded", "row_count": 1},
            {"query": "exclude_update_boundary", "status": "empty", "row_count": 0},
        ],
    }


def test_revision_sample_filter_success_is_at_most_degraded_even_if_input_claims_passed() -> None:
    source = {"status": "passed", "financial_revision_probe": _revision_probe()}
    result = DiagnosticResult("research_source_compare", 0, 1.0, source, None)
    normalized = NormalizeActionResult("research-source-compare", result)
    assert NormalizeActionResult("research-source-compare", normalized) == normalized
    report = BuildReport("research-source-compare", (normalized,))
    assert report["status"] == "degraded"
    assert report["checks"][0]["financial_revision_probe"]["update_filter_verified"]


@pytest.mark.parametrize("change", (
    {"read_only": False}, {"production_adopted": True}, {"field_correctness_verified": True},
    {"full_window_coverage_verified": True}, {"sort_verified": True},
    {"request_count": 4}, {"queries": _revision_probe()["queries"] * 2},
    {"request_count": True}, {"request_limit": 4}, {"max_response_bytes": 2 * 1024 * 1024},
    {"queries": [{"query": "baseline", "status": "decoded", "row_count": 1}] * 3},
    {"update_filter_verified": False},
))
def test_revision_invalid_claims_or_unbounded_attempts_fail_closed(change) -> None:
    source = {"status": "passed", "financial_revision_probe": dict(_revision_probe(), **change)}
    report = BuildReport("research-source-compare", (DiagnosticResult("research_source_compare", 0, 1.0, source, None),))
    assert report["status"] == "failed"
    projected = report["checks"][0]["financial_revision_probe"]
    assert not any(projected[name] for name in ("production_adopted", "field_correctness_verified", "full_window_coverage_verified", "sort_verified"))


def test_revision_failed_status_is_preserved_even_with_valid_sample_evidence() -> None:
    source = {"status": "failed", "financial_revision_probe": _revision_probe()}
    report = BuildReport("research-source-compare", (DiagnosticResult("research_source_compare", 1, 1.0, source, None),))
    assert report["status"] == "failed"


@pytest.mark.parametrize("code,success,expected_code,expected_success", (
    (9501, False, 9501, False), (True, "PRIVATE_PROVIDER_PAYLOAD", None, None),
))
def test_revision_production_fields_control_is_separate_and_provider_status_is_typed(code, success, expected_code, expected_success) -> None:
    probe = dict(_revision_probe(), support_status="not_verified", update_filter_verified=False, request_count=2)
    probe["queries"] = [
        {"query": "baseline", "status": "failed", "provider_code": code, "provider_success": success},
        {"query": "production_fields_control", "status": "decoded", "provider_code": 0, "provider_success": True, "row_count": 1},
    ]
    probe["production_fields_control"] = {
        "status": "decoded", "identity": "600001.SH", "report_date": "2026-06-30", "notice_at": "2026-08-20T00:00:00+08:00",
        "update_date_requested": False, "revision_verified": False,
        "values": {"eps": 1.25, "raw": "PRIVATE_PROVIDER_PAYLOAD"},
        "updated_at": "PRIVATE_PROVIDER_PAYLOAD", "raw": "PRIVATE_PROVIDER_PAYLOAD",
    }
    source = {"status": "failed", "financial_revision_probe": probe}
    report = BuildReport("research-source-compare", (DiagnosticResult("research_source_compare", 1, 1.0, source, None),))
    projected = report["checks"][0]["financial_revision_probe"]
    assert report["status"] == "failed" and not projected["update_filter_verified"] and "record" not in projected
    assert projected["queries"][0]["provider_code"] == expected_code
    assert projected["queries"][0]["provider_success"] is expected_success
    control = projected["production_fields_control"]
    assert control["status"] == "decoded" and control["values"]["eps"] == 1.25
    assert "updated_at" not in control and not control["update_date_requested"] and not control["revision_verified"]
    assert "PRIVATE_PROVIDER_PAYLOAD" not in json.dumps(report)


def test_revision_production_control_can_never_be_reported_as_successful_update_verification() -> None:
    probe = dict(_revision_probe(), production_fields_control={"status": "decoded"})
    source = {"status": "passed", "financial_revision_probe": probe}
    report = BuildReport("research-source-compare", (DiagnosticResult("research_source_compare", 0, 1.0, source, None),))
    assert report["status"] == "failed"
