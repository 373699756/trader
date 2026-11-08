"""Public reporting preserves bounded candidate evidence without adopting a source."""

import json
import re
import time
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from scripts.core.contracts import DiagnosticResult
from scripts.core.projections import ResultStatus
from scripts.download.company.company_st_source_audit import AuditCompanyStSources, CompanyStSourceRequest
from scripts.download.company.reporting import NormalizeResult, ProjectDetails
from trader.infra.http_contracts import HttpResponse


class _Session:
    def get(self, url, **kwargs):
        code = re.search(r"[036][0-9]{5}", url)[0]
        if "PageAjax" in url:
            return HttpResponse(200, json.dumps({"jbzl": [{
                "SECURITY_CODE": code, "SECURITY_NAME_ABBR": "样本", "STR_NAMEA": "样本", "FORMERNAME": "ST样本",
            }]}).encode())
        return HttpResponse(200, "<td>证券简称更名历史：</td><td>ST样本</td>".encode("gb18030"))


def _report(count=1, rounds=3):
    return AuditCompanyStSources(
        tuple(CompanyStSourceRequest(f"600{number:03}", "样本", "ever_st") for number in range(1, count + 1)),
        session_factory=lambda: nullcontext(_Session()), rounds=rounds, workers=2,
        deadline=time.monotonic() + 30,
        clock=lambda: datetime(2026, 11, 8, 12, tzinfo=ZoneInfo("Asia/Shanghai")),
    )


def _project(report):
    normalized = NormalizeResult(DiagnosticResult("research_source_compare", 0, 10.0, report, None))
    output = {}
    ProjectDetails(normalized, normalized.payload, output)
    return normalized, output["summary"]


@pytest.mark.parametrize("count,rounds,expected", [(42, 3, 168), (64, 5, 384)])
def test_native_st_report_keeps_every_selected_observation_without_claiming_verification(count, rounds, expected):
    report = _report(count, rounds)
    report["reference_hash"] = "a" * 64
    normalized, summary = _project(report)
    assert ResultStatus(normalized) == "degraded" and summary["observation_status"] == "observed"
    assert len(summary["observations"]) == expected and not summary["observations_truncated"]
    assert len(summary["comparisons"]) == count and summary["actual_requests"] == expected
    assert summary["reference_hash"] == "a" * 64 and summary["observed_at"].endswith("+08:00")
    assert summary["production_adopted"] is summary["field_correctness_verified"] is summary["full_market_verified"] is False
    assert summary["sources"][0]["actual_requests"] == count * rounds
    assert summary["sources"][1]["actual_requests"] == count
    assert "schema_version" not in summary


@pytest.mark.parametrize("status", ["degraded", "cancelled", "timed_out"])
def test_native_degraded_cancelled_and_timeout_outcomes_are_preserved(status):
    report = _report()
    report["status"] = status
    normalized, summary = _project(report)
    assert ResultStatus(normalized) == status and summary["observation_status"] == status


def test_repeated_normalization_preserves_the_native_observed_identity():
    normalized, summary = _project(_report())
    repeated, repeated_summary = _project(normalized.payload)
    assert repeated.payload == normalized.payload
    assert summary == repeated_summary and repeated_summary["observation_status"] == "observed"


@pytest.mark.parametrize("field,value", [
    ("production_adopted", True), ("field_correctness_verified", True), ("full_market_verified", True),
    ("source_data_writes", 1), ("source_data_writes", False), ("read_only", False),
    ("workers", 5), ("rounds", 6), ("actual_requests", 385), ("status", "passed"),
    ("schema_version", "private_format"), ("reference_hash", "https://private.invalid/?token=private"),
])
def test_invalid_safety_claims_and_budgets_are_failed(field, value):
    report = _report()
    report[field] = value
    normalized, _summary = _project(report)
    assert ResultStatus(normalized) == "failed"


def test_projection_does_not_forward_unknown_company_fields_contacts_paths_or_query_secrets():
    report = _report()
    report["company_profile"] = {"CONTACT_TEL": "PRIVATE_CONTACT", "ORG_PROFILE": "PRIVATE_PAYLOAD"}
    observation = report["observations"][0]
    observation["current_name"] = "样本 https://user:token@private.invalid/?private=raw name@private.invalid 13800138000"
    observation["former_names_excerpt"] = "ST样本 https://private.invalid/?private=raw " + "长名称" * 100
    observation["document_url"] = "https://user:token@private.invalid/?private=raw"
    observation["diagnostic"] = "RuntimeError /private/company.pdf?private=raw"
    observation["reason"] = "http_500?private=raw"
    observation["structure"]["fields"]["CONTACT_TEL"] = "PRIVATE_CONTACT"
    report["sources"][0]["confusion"]["PRIVATE_CONTACT"] = {"raw": "PRIVATE_PAYLOAD"}
    normalized, summary = _project(report)
    text = json.dumps(summary, ensure_ascii=False)
    assert ResultStatus(normalized) == "degraded"
    assert all(value not in text for value in ("PRIVATE_CONTACT", "PRIVATE_PAYLOAD", "private.invalid", "/private/", "private=raw", "13800138000"))
    assert len(summary["observations"][0]["former_names_excerpt"]) <= 160
    assert summary["observations"][0]["diagnostic"] is summary["observations"][0]["reason"] is None


def test_observation_limit_is_explicit_and_malformed_status_does_not_escape_as_json():
    report = _report(64, 5)
    report["observations"].append(deepcopy(report["observations"][0]))
    report["status"] = {"PRIVATE_PAYLOAD": "contact"}
    normalized, summary = _project(report)
    assert ResultStatus(normalized) == "failed" and summary["observation_status"] is None
    assert len(summary["observations"]) == 384 and summary["observations_truncated"]


def test_strict_c_prefix_name_difference_keeps_safe_observed_identity_in_public_projection():
    class IdentitySession:
        def get(self, url, **kwargs):
            if "PageAjax" in url:
                return HttpResponse(200, json.dumps({"jbzl": [{
                    "SECURITY_CODE": "001381", "SECURITY_NAME_ABBR": "皇冠新材", "STR_NAMEA": "皇冠新材", "FORMERNAME": "--",
                }]}).encode())
            return HttpResponse(200, "<td>证券简称更名历史：</td><td>皇冠新材</td>".encode("gb18030"))

    report = AuditCompanyStSources((CompanyStSourceRequest("001381", "C皇冠新材", "clear"),),
        session_factory=lambda: nullcontext(IdentitySession()), rounds=1, workers=1,
        deadline=time.monotonic() + 30)
    normalized, summary = _project(report)
    candidate = summary["observations"][0]
    assert ResultStatus(normalized) == "degraded"
    assert candidate["current_name"] == "C皇冠新材" and candidate["reason"] == "eastmoney_current_name_mismatch"
    assert candidate["structure"]["observed_code"] == "001381"
    assert candidate["structure"]["observed_current_name"] == candidate["structure"]["observed_a_name"] == "皇冠新材"


def test_observed_identity_whitelist_normalizes_names_and_removes_sensitive_strings():
    report = _report(rounds=1)
    report["observations"][0]["structure"].update({
        "observed_code": "001381?private=raw", "observed_current_name": "公司 https://x/?private=raw",
        "observed_a_name": "京东方Ａ", "CONTACT_TEL": "PRIVATE_CONTACT",
    })
    _normalized, summary = _project(report)
    structure = summary["observations"][0]["structure"]
    assert structure["observed_code"] is None and structure["observed_a_name"] == "京东方A"
    assert structure["observed_current_name"] == "公司 [省略]"
    assert "private=raw" not in json.dumps(summary) and "PRIVATE_CONTACT" not in json.dumps(summary)
