"""Structured source samples remain bounded observations with no production writes."""

import json
from contextlib import nullcontext
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from scripts.checks.download.company import company_structured_field_audit as audit
from trader.download.domain.company_annual_income import CompanyAnnualIncomeRequest
from trader.infra.http_contracts import HttpConnectionError, HttpResponse

_NOW = datetime(2026, 11, 5, 12, tzinfo=ZoneInfo("Asia/Shanghai"))


def _payload(source):
    row = {field: 123.5 for field in source.numeric_fields}
    row.update({field: "fixture" for field in source.evidence_fields})
    row.update({source.code_column: "600001", source.date_column: "2026-11-04 00:00:00"})
    if source.name == "annual_income":
        row["REPORT_DATE"] = "2025-12-31 00:00:00"
    row["UNRELATED_PROVIDER_FIELD"] = "private-response-body"
    return {"code": 0, "success": True, "result": {"count": 501, "pages": 101, "data": [row]}}


class _Session:
    def __init__(self):
        self.requests = []
        self.payloads = {source.report: _payload(source) for source in audit._SOURCES}
        self.failure = None

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        report = kwargs["params"]["reportName"]
        if report == self.failure:
            raise HttpConnectionError("private-provider-error")
        return HttpResponse(200, json.dumps(self.payloads[report]).encode())


def _run(session, **kwargs):
    return audit.AuditCompanyStructuredSources(
        session,
        observed_at=_NOW,
        timeout_seconds=8,
        deadline=60,
        monotonic=kwargs.get("monotonic", lambda: 0),
    )


def test_annual_probe_uses_production_decoder_and_projects_only_annual_reference_fields():
    class Session:
        def __init__(self):
            self.calls = 0

        def get(self, url, **kwargs):
            self.calls += 1
            if url.endswith("/Index"):
                return HttpResponse(200, b'<input id="hidctype" value="4">')
            return HttpResponse(200, json.dumps({"data": [{
                "SECUCODE": "600001.SH", "REPORT_DATE": "2025-12-31", "REPORT_TYPE": "年报",
                "NOTICE_DATE": "2026-04-17", "UPDATE_DATE": "2026-04-17", "CURRENCY": "CNY",
                "OPERATE_INCOME": 123, "TOTAL_OPERATE_INCOME": 456, "UNRELATED": "private-payload",
            }]}).encode())

    session = Session()
    result = audit.AuditCompanyAnnualIncome(
        session, CompanyAnnualIncomeRequest("600001", (date(2025, 12, 31),)), observed_at=_NOW,
        timeout_seconds=8, deadline=60, monotonic=lambda: 0,
    )
    assert session.calls == 2 and result["status"] == "decoded" and result["read_only"]
    assert result["records"][0]["operating_revenue"] == 123
    assert "private-payload" not in json.dumps(result) and "TOTAL_OPERATE_INCOME" not in json.dumps(result)
    assert not result["field_correctness_verified"] and not result["full_window_coverage_verified"]


def test_annual_probe_rejects_expired_budget_before_io():
    with pytest.raises(ValueError, match="company_annual_probe_deadline_invalid"):
        audit.AuditCompanyAnnualIncome(
            _Session(), CompanyAnnualIncomeRequest("600001", (date(2025, 12, 31),)), observed_at=_NOW,
            timeout_seconds=8, deadline=0, monotonic=lambda: 0,
        )


def test_four_structured_sources_sample_one_page_each_without_exposing_provider_rows():
    session = _Session()
    result = _run(session)
    assert result["status"] == "passed" and result["read_only"]
    assert not result["production_adopted"] and not result["field_correctness_verified"]
    assert not result["full_window_coverage_verified"]
    assert len(session.requests) == 4
    for _url, request in session.requests:
        parameters = request["params"]
        assert parameters["pageSize"] == "5" and parameters["pageNumber"] == "1"
        assert parameters["columns"] != "ALL" and "UNRELATED_PROVIDER_FIELD" not in parameters["columns"]
        assert "2025-11-05" in parameters["filter"] and "2026-11-05" in parameters["filter"]
        assert request["timeout"] == 8 and request["deadline"] == 60
        assert request["max_response_bytes"] == 1024 * 1024 and not request["cancel_requested"]()
    income = session.requests[2][1]["params"]
    assert "REPORT_DATE='2025-12-31'" in income["filter"]
    for row in result["sources"]:
        assert row["status"] == "sampled" and row["rows"] == 1
        assert row["reported_count"] == 501 and row["reported_pages"] == 101
        assert len(row["response_sha256"]) == 64
    assert "private-response-body" not in json.dumps(result)
    assert "UNRELATED_PROVIDER_FIELD" not in json.dumps(result)


@pytest.mark.parametrize("kind", ("old_notice", "future_notice", "wrong_annual_period", "missing_numeric_field"))
def test_ignored_filters_and_missing_required_fields_cannot_pass_the_probe(kind):
    session = _Session()
    source = audit._SOURCES[2] if kind == "wrong_annual_period" else audit._SOURCES[0]
    row = session.payloads[source.report]["result"]["data"][0]
    if kind == "old_notice":
        row[source.date_column] = "2025-11-04 00:00:00"
    elif kind == "future_notice":
        row[source.date_column] = "2026-11-06 00:00:00"
    elif kind == "wrong_annual_period":
        row["REPORT_DATE"] = "2026-09-30 00:00:00"
    else:
        row.pop(source.numeric_fields[0])
    result = _run(session)
    sample = next(row for row in result["sources"] if row["report"] == source.report)
    assert sample["status"] == "invalid" and result["status"] == "degraded"
    assert len(session.requests) == 4


@pytest.mark.parametrize("kind", ("count_bool", "too_many_rows", "date_object", "provider_failure", "bool_code"))
def test_malformed_provider_responses_have_bounded_failures_and_other_sources_continue(kind):
    session = _Session()
    payload = session.payloads[audit._SOURCES[0].report]
    if kind == "count_bool":
        payload["result"]["count"] = True
    elif kind == "too_many_rows":
        payload["result"]["data"] *= 6
    elif kind == "date_object":
        payload["result"]["data"][0]["UPDATEDATE"] = {}
    elif kind == "bool_code":
        payload["code"] = False
    else:
        payload.update({"code": 123, "message": "private-provider-error"})
    result = _run(session)
    assert result["status"] == "degraded" and result["sources"][0]["status"] == "failed"
    assert result["sources"][0]["failure"] == "ValueError"
    assert all(row["status"] == "sampled" for row in result["sources"][1:])
    assert "private-provider-error" not in json.dumps(result)


def test_transport_failure_is_preserved_as_a_category_without_replacing_successful_sources():
    session = _Session()
    session.failure = audit._SOURCES[1].report
    result = _run(session)
    assert result["sources"][1]["failure"] == "HttpConnectionError"
    assert result["sources"][0]["status"] == result["sources"][2]["status"] == "sampled"
    assert "private-provider-error" not in json.dumps(result)


def test_deadline_skips_all_requests_and_does_not_claim_an_empty_success():
    session = _Session()
    result = _run(session, monotonic=lambda: 60)
    assert session.requests == [] and result["status"] == "degraded"
    assert all(row["status"] == "skipped" and row["reason"] == "task_deadline" for row in result["sources"])


@pytest.mark.parametrize("limit", (0, 501, True))
def test_invalid_sample_limit_is_refused_before_network_io(limit):
    session = _Session()
    with pytest.raises(ValueError, match="sample_limit_invalid"):
        audit.AuditCompanyStructuredSources(
            session,
            observed_at=_NOW,
            timeout_seconds=8,
            deadline=60,
            sample_rows=limit,
            monotonic=lambda: 0,
        )
    assert not session.requests


def test_explicit_larger_sample_keeps_the_same_response_and_request_boundaries():
    session = _Session()
    for payload in session.payloads.values():
        payload["result"]["data"] *= 8
    result = audit.AuditCompanyStructuredSources(
        session,
        observed_at=_NOW,
        timeout_seconds=8,
        deadline=60,
        sample_rows=500,
        monotonic=lambda: 0,
    )
    assert result["status"] == "passed" and len(session.requests) == 4
    assert all(request["params"]["pageSize"] == "500" for _url, request in session.requests)
    assert all(row["rows"] == 8 for row in result["sources"])


def test_schema_discovery_reads_one_row_and_exposes_only_column_names_and_projections():
    session = _Session()
    result = audit.AuditCompanyStructuredSources(
        session, observed_at=_NOW, timeout_seconds=8, deadline=60, sample_rows=1,
        discover_schema=True, monotonic=lambda: 0,
    )
    assert result["schema_discovery_only"] and result["status"] == "passed"
    assert len(session.requests) == 4
    assert all(request["params"]["columns"] == "ALL" and request["params"]["pageSize"] == "1"
               for _url, request in session.requests)
    assert all("UNRELATED_PROVIDER_FIELD" in row["returned_fields"] for row in result["sources"])
    assert "private-response-body" not in json.dumps(result)
    holder = next(source for source in audit._SOURCES if source.name == "holder_trade")
    assert "AFTER_CHANGE_RATE" in holder.numeric_fields and "CHANGE_RATE" not in holder.numeric_fields


@pytest.mark.parametrize("limit", (5, 500))
def test_schema_discovery_cannot_be_expanded_into_a_bulk_payload_request(limit):
    session = _Session()
    with pytest.raises(ValueError, match="schema_limit_invalid"):
        audit.AuditCompanyStructuredSources(
            session, observed_at=_NOW, timeout_seconds=8, deadline=60, sample_rows=limit,
            discover_schema=True, monotonic=lambda: 0,
        )
    assert not session.requests


@pytest.mark.parametrize("value", (True, "1.5", float("inf"), float("nan"), 10**500))
def test_unverified_numeric_values_are_not_counted_as_valid_numbers(value):
    assert not audit._finite_number(value)


def test_existing_source_check_routes_the_explicit_field_mode_without_running_directory_discovery(monkeypatch, capsys):
    from scripts.checks.download.company import research_source_compare as entrypoint

    monkeypatch.setattr(entrypoint, "OpenExternalHttp", lambda: nullcontext(_Session()))
    monkeypatch.setattr(entrypoint, "Collect", lambda args: pytest.fail("directory discovery must stay unselected"))
    monkeypatch.setattr(entrypoint, "AuditCompanyStructuredSources", lambda *args, **kwargs: {"read_only": True})
    monkeypatch.setattr("sys.argv", ["research-source-compare", "--config", "config/runtime.json", "--codes", "600001", "--field-sources-only"])
    assert entrypoint.Main() == 0
    assert json.loads(capsys.readouterr().out) == {"read_only": True}
