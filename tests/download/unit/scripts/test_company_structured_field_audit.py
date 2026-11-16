"""Structured source samples remain bounded observations with no production writes."""

import hashlib
import json
import re
from contextlib import nullcontext
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from scripts.download.company import company_structured_field_audit as audit
from trader.download.domain.company_annual_income import CompanyAnnualIncomeRequest
from trader.download.domain.company_evidence import StructuredCompanyQuery
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


def _revision_payload(**changes):
    row = {
        "SECUCODE": "600001.SH", "REPORT_DATE": "2026-06-30 00:00:00",
        "NOTICE_DATE": "2026-08-20 00:00:00", "UPDATE_DATE": "2026-10-30 10:00:00",
        "EPSJB": "1.25", "BPS": 2, "TOTALOPERATEREVETZ": 3, "PARENTNETPROFITTZ": 4,
        "KCFJCXSYJLRTZ": 5, "ROEJQ": 6, "PARENTNETPROFIT": 7, "KCFJCXSYJLR": None,
        "UNRELATED_PROVIDER_FIELD": "private-provider-payload", **changes,
    }
    return {"success": True, "code": 0, "result": {"count": 1, "pages": 1, "data": [row]}}


class _RevisionSession:
    def __init__(self, payload=None):
        self.payload = _revision_payload() if payload is None else payload
        self.requests = []
        self.responses = []
        self.ignore_filter = False
        self.failure = None

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        if self.failure is not None:
            raise self.failure
        excluded = "UPDATE_DATE<'" in kwargs["params"]["filter"]
        payload = {"success": True, "code": 0, "result": {"count": 0, "pages": 0, "data": None}} if excluded and not self.ignore_filter else self.payload
        response = HttpResponse(200, json.dumps(payload).encode())
        self.responses.append(response)
        return response


def _revision_run(session, **kwargs):
    return audit.AuditCompanyFinancialRevision(
        session, audit.CompanyFinancialRevisionRequest("600001", date(2026, 6, 30)),
        observed_at=_NOW, timeout_seconds=8, deadline=60, clock=lambda: _NOW,
        monotonic=kwargs.get("monotonic", lambda: 0),
    )


def test_revision_probe_verifies_an_excluding_boundary_and_retains_original_notice_and_eight_metrics():
    session = _RevisionSession()
    report = _revision_run(session)
    probe = report["financial_revision_probe"]
    assert report["status"] == "degraded" and probe["support_status"] == "sample_verified"
    assert probe["update_filter_verified"] and probe["read_only"]
    assert not any(probe[name] for name in ("production_adopted", "field_correctness_verified", "full_window_coverage_verified", "sort_verified"))
    assert "schema_version" not in report
    assert probe["record"] == {
        "identity": "600001.SH", "report_date": "2026-06-30",
        "notice_at": "2026-08-20T00:00:00+08:00", "updated_at": "2026-10-30T10:00:00+08:00",
        "values": {"eps": 1.25, "bps": 2.0, "revenue_growth_pct": 3.0, "profit_growth_pct": 4.0,
                   "core_profit_growth_pct": 5.0, "roe_pct": 6.0, "parent_profit": 7.0, "core_profit": None},
        "missing_value_fields": ("core_profit",),
    }
    assert len(session.requests) == probe["request_count"] == probe["request_limit"] == 3
    for index, (url, request) in enumerate(session.requests):
        assert url == "https://datacenter.eastmoney.com/securities/api/data/get"
        assert request["headers"] == {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}
        parameters = request["params"]
        assert parameters["type"] == "RPT_F10_FINANCE_MAINFINADATA" and parameters["p"] == parameters["ps"] == "1"
        assert set(parameters["sty"].split(",")) == {
            "SECUCODE", "REPORT_DATE", "NOTICE_DATE", "UPDATE_DATE", "EPSJB", "BPS", "TOTALOPERATEREVETZ",
            "PARENTNETPROFITTZ", "KCFJCXSYJLRTZ", "ROEJQ", "PARENTNETPROFIT", "KCFJCXSYJLR",
        }
        assert "(SECUCODE='600001.SH')(REPORT_DATE='2026-06-30')" in parameters["filter"]
        assert request["deadline"] == 60 and request["timeout"] == 8 and request["max_response_bytes"] == 1024 * 1024
        assert not request["cancel_requested"]()
        assert probe["queries"][index]["response_sha256"] == hashlib.sha256(session.responses[index].content).hexdigest()
        assert probe["queries"][index]["received_at"] == _NOW.isoformat()
    assert "UPDATE_DATE>='2026-10-30'" in session.requests[1][1]["params"]["filter"]
    assert "UPDATE_DATE<'2026-10-30'" in session.requests[2][1]["params"]["filter"]
    assert probe["queries"][-1]["row_count"] == 0
    assert "private-provider-payload" not in json.dumps(report)


def test_revision_inclusion_alone_cannot_verify_an_ignored_update_filter():
    session = _RevisionSession()
    session.ignore_filter = True
    report = _revision_run(session)
    probe = report["financial_revision_probe"]
    assert report["status"] == "failed" and not probe["update_filter_verified"]
    assert probe["failure"] == "company_financial_revision_filter_ignored"
    assert probe["request_count"] == 3 and probe["record"]["notice_at"].startswith("2026-08-20")


@pytest.mark.parametrize("changes,failure", (
    ({"SECUCODE": "000001.SZ"}, "identity_mismatch"),
    ({"REPORT_DATE": "2026-03-31"}, "identity_mismatch"),
    ({"NOTICE_DATE": "2026-01-01"}, "time_invalid"),
    ({"UPDATE_DATE": "2027-01-01"}, "time_invalid"),
    ({"UPDATE_DATE": "2026-07-01"}, "time_invalid"),
    ({"EPSJB": True}, "number_invalid"),
    ({"EPSJB": "NaN"}, "number_invalid"),
    ({"EPSJB": 10**500}, "number_invalid"),
))
def test_revision_wrong_identity_time_and_invalid_numeric_values_stop_before_filter_claim(changes, failure):
    session = _RevisionSession(_revision_payload(**changes))
    probe = _revision_run(session)["financial_revision_probe"]
    assert probe["failure"] == "company_financial_revision_" + failure
    assert not probe["update_filter_verified"] and probe["request_count"] == 1


@pytest.mark.parametrize("kind", ("missing_update", "missing_metric", "count_two", "count_bool", "pages_two", "provider_error", "empty"))
def test_revision_unavailable_columns_counts_and_provider_errors_are_explicit_failures(kind):
    payload = _revision_payload()
    if kind.startswith("missing_"):
        payload["result"]["data"][0].pop("UPDATE_DATE" if kind == "missing_update" else "BPS")
    elif kind == "count_two":
        payload["result"]["count"] = 2
    elif kind == "count_bool":
        payload["result"]["count"] = True
    elif kind == "pages_two":
        payload["result"]["pages"] = 2
    elif kind == "provider_error":
        payload.update({"success": False, "message": "private-provider-error"})
    else:
        payload["result"] = {"count": 0, "pages": 0, "data": []}
    session = _RevisionSession(payload)
    report = _revision_run(session)
    probe = report["financial_revision_probe"]
    assert report["status"] == "failed" and probe["support_status"] == "not_verified"
    assert not probe["update_filter_verified"] and probe["request_count"] == (2 if kind == "provider_error" else 1)
    assert "private-provider-error" not in json.dumps(report)


class _RevisionControlSession(_RevisionSession):
    def __init__(self, control=None):
        super().__init__({"success": False, "code": 9501, "message": "private-provider-error"})
        self.control = _revision_payload() if control is None else control
        if control is None:
            self.control["result"]["data"][0].pop("UPDATE_DATE")

    def get(self, url, **kwargs):
        if "UPDATE_DATE" not in kwargs["params"]["sty"].split(","):
            self.payload = self.control
        return super().get(url, **kwargs)


def test_revision_rejection_adds_only_a_production_fields_control_without_inventing_update_time():
    session = _RevisionControlSession()
    report = _revision_run(session)
    probe = report["financial_revision_probe"]
    assert report["status"] == "failed" and probe["support_status"] == "not_verified"
    assert probe["failure"] == "company_financial_revision_provider_rejected"
    assert not probe["update_filter_verified"] and "record" not in probe
    control = probe["production_fields_control"]
    assert control["status"] == "decoded" and control["identity"] == "600001.SH"
    assert control["report_date"] == "2026-06-30" and control["notice_at"].startswith("2026-08-20")
    assert len(control["values"]) == 8 and control["values"]["eps"] == 1.25
    assert not control["update_date_requested"] and not control["revision_verified"] and "updated_at" not in control
    assert probe["request_count"] == len(session.requests) == 2
    assert probe["queries"][0]["provider_code"] == 9501 and probe["queries"][0]["provider_success"] is False
    assert probe["queries"][1]["provider_code"] == 0 and probe["queries"][1]["provider_success"] is True
    assert probe["queries"][1]["query"] == "production_fields_control"
    baseline, original = (request["params"] for _url, request in session.requests)
    assert original["filter"] == baseline["filter"]
    assert original["sty"].split(",") == [field for field in baseline["sty"].split(",") if field != "UPDATE_DATE"]
    assert all(request["deadline"] == 60 and request["max_response_bytes"] == 1024 * 1024 for _url, request in session.requests)
    assert "private-provider-error" not in json.dumps(report)


@pytest.mark.parametrize("control", (
    {"success": False, "code": -1, "message": "private-provider-error"},
    {"success": True, "code": 0, "result": {"count": 0, "pages": 0, "data": []}},
    _revision_payload(SECUCODE="000001.SZ"),
))
def test_revision_control_rejection_absence_and_wrong_identity_never_verify_the_update_protocol(control):
    session = _RevisionControlSession(control)
    report = _revision_run(session)
    probe = report["financial_revision_probe"]
    assert report["status"] == "failed" and not probe["update_filter_verified"]
    assert probe["support_status"] == "not_verified" and "record" not in probe
    assert probe["production_fields_control"]["status"] in ("empty", "failed")
    assert probe["request_count"] == 2 and "updated_at" not in probe["production_fields_control"]
    assert "private-provider-error" not in json.dumps(report)


def test_revision_control_shares_the_deadline_and_cannot_start_another_attempt_after_expiry():
    session = _RevisionControlSession()
    current = [0]
    calls = [0]

    def monotonic():
        if session.requests:
            calls[0] += 1
            if calls[0] >= 2:
                current[0] = 60
        return current[0]

    probe = _revision_run(session, monotonic=monotonic)["financial_revision_probe"]
    assert probe["failure"] == "company_financial_revision_provider_rejected"
    assert probe["production_fields_control"]["failure"] == "company_financial_revision_deadline_exhausted"
    assert probe["request_count"] == len(session.requests) == 1


@pytest.mark.parametrize("code,success", ((False, 1), ("PRIVATE_PROVIDER_PAYLOAD", "PRIVATE_PROVIDER_PAYLOAD")))
def test_revision_provider_status_only_projects_native_int_and_bool_values(code, success):
    session = _RevisionSession({"success": success, "code": code, "message": "PRIVATE_PROVIDER_PAYLOAD"})
    probe = _revision_run(session)["financial_revision_probe"]
    assert probe["request_count"] == 2
    assert all(query["provider_code"] is None and query["provider_success"] is None for query in probe["queries"])
    assert "PRIVATE_PROVIDER_PAYLOAD" not in json.dumps(probe)


def test_revision_transport_error_is_one_attempt_without_exposing_its_message():
    session = _RevisionSession()
    session.failure = HttpConnectionError("private-provider-error")
    report = _revision_run(session)
    assert report["status"] == "failed" and report["financial_revision_probe"]["failure"] == "HttpConnectionError"
    assert len(session.requests) == report["financial_revision_probe"]["request_count"] == 1
    assert "private-provider-error" not in json.dumps(report)


def test_revision_external_exception_cannot_impersonate_a_public_protocol_reason():
    session = _RevisionSession()
    session.failure = ValueError("company_financial_revision_private_provider_payload")
    report = _revision_run(session)
    assert report["financial_revision_probe"]["failure"] == "ValueError"
    assert "private_provider_payload" not in json.dumps(report)


def test_revision_future_period_is_rejected_before_any_http_request():
    session = _RevisionSession()
    with pytest.raises(ValueError, match="options_invalid"):
        audit.AuditCompanyFinancialRevision(
            session, audit.CompanyFinancialRevisionRequest("600001", date(2026, 12, 31)),
            observed_at=_NOW, timeout_seconds=8, deadline=60, clock=lambda: _NOW, monotonic=lambda: 0,
        )
    assert not session.requests


@pytest.mark.parametrize("expired", (True, False))
def test_revision_shared_deadline_prevents_io_or_followup_after_budget_exhaustion(expired):
    session = _RevisionSession()
    current = [60 if expired else 0]
    original_get = session.get

    def get(url, **kwargs):
        response = original_get(url, **kwargs)
        current[0] = 60
        assert kwargs["cancel_requested"]()
        return response

    session.get = get
    report = _revision_run(session, monotonic=lambda: current[0])
    assert report["status"] == "failed"
    assert report["financial_revision_probe"]["failure"] == "company_financial_revision_deadline_exhausted"
    assert len(session.requests) == (0 if expired else 1)


def test_revision_response_bytes_are_bounded_even_when_an_injected_transport_ignores_its_limit():
    class Session:
        def get(self, url, **kwargs):
            return HttpResponse(200, b"x" * (1024 * 1024 + 1))

    report = _revision_run(Session())
    assert report["status"] == "failed"
    assert report["financial_revision_probe"]["failure"] == "company_financial_revision_response_size_invalid"
    assert report["financial_revision_probe"]["request_count"] == 1


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


def test_identity_samples_are_explicit_bounded_and_only_project_whitelisted_finite_values():
    session = _Session()
    row = session.payloads[audit._SOURCES[0].report]["result"]["data"][0]
    row["REPUROBJECTIVE"] = "x" * 1000
    row["REPURNUM"] = float("inf")
    row["REPURAMOUNT"] = True
    result = audit.AuditCompanyStructuredSources(
        session, observed_at=_NOW, timeout_seconds=8, deadline=60,
        sample_rows=3, include_identities=True, monotonic=lambda: 0,
    )
    projected = result["sources"][0]["identity_samples"][0]
    assert projected["REPUROBJECTIVE"] == "x" * 160
    assert "REPURNUM" not in projected and "REPURAMOUNT" not in projected
    assert "UNRELATED_PROVIDER_FIELD" not in json.dumps(result)
    assert "private-response-body" not in json.dumps(result)
    assert all(len(source["identity_samples"]) <= 3 for source in result["sources"])


@pytest.mark.parametrize("rows", (6, 500))
def test_identity_sample_limit_is_checked_before_any_request(rows):
    session = _Session()
    with pytest.raises(ValueError, match="identity_limit"):
        audit.AuditCompanyStructuredSources(
            session, observed_at=_NOW, timeout_seconds=8, deadline=60,
            sample_rows=rows, include_identities=True, monotonic=lambda: 0,
        )
    assert not session.requests


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
    from scripts.download.company import research_source_compare as entrypoint

    monkeypatch.setattr(entrypoint, "OpenExternalHttp", lambda: nullcontext(_Session()))
    monkeypatch.setattr(entrypoint, "Collect", lambda args: pytest.fail("directory discovery must stay unselected"))
    monkeypatch.setattr(entrypoint, "AuditCompanyStructuredSources", lambda *args, **kwargs: {"read_only": True})
    configuration = Path(__file__).resolve().parents[4] / "config/runtime.json"
    monkeypatch.setattr("sys.argv", ["research-source-compare", "--config", str(configuration), "--field-sources-only"])
    assert entrypoint.Main() == 0
    assert json.loads(capsys.readouterr().out) == {
        "read_only": True,
        "runtime_configuration_sha256": hashlib.sha256(configuration.read_bytes()).hexdigest(),
    }


class _StructuredPageSession:
    def __init__(self, rows, *, total=None, pages=1):
        self.requests = []
        self.payload = {"code": 0, "success": True, "result": {"count": len(rows) if total is None else total, "pages": pages, "data": rows}}

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return HttpResponse(200, json.dumps(self.payload).encode())


def _page_run(session, query, **changes):
    return audit.AuditCompanyStructuredPage(
        session, query, observed_at=_NOW, timeout_seconds=8, deadline=60,
        monotonic=changes.pop("monotonic", lambda: 0), **changes,
    )


def test_structured_financial_page_consumes_all_eight_fields_from_one_response_without_followup():
    row = {
        "SECUCODE": "600001.SH", "REPORT_DATE": "2026-09-30", "NOTICE_DATE": "2026-11-04",
        "EPSJB": 0, "BPS": None, "TOTALOPERATEREVETZ": 10, "PARENTNETPROFITTZ": -5,
        "KCFJCXSYJLRTZ": 3, "ROEJQ": 0, "PARENTNETPROFIT": -1, "KCFJCXSYJLR": 0,
        "RAW_SECRET": "private-provider-data",
    }
    session = _StructuredPageSession([row, dict(row, SECUCODE="000001.SZ"), dict(row, SECUCODE="00001.HK")], total=1001, pages=3)
    result = _page_run(session, StructuredCompanyQuery("financial", date(2026, 11, 4), _NOW.date()))
    assert len(session.requests) == result["fetch_invocations"] == 1
    parameters = session.requests[0][1]["params"]
    assert parameters["sty"].split(",") == result["requested_fields"]
    assert parameters["ps"] == "500" and parameters["p"] == "1" and parameters["quoteColumns"] == ""
    assert parameters["filter"] == "(NOTICE_DATE>='2026-11-04')(NOTICE_DATE<='2026-11-05')"
    assert result["raw_rows"] == 3 and result["decoded_rows"] == 2
    assert result["reported_total"] == 1001 and result["reported_pages"] == 3
    assert result["more_pages_available"] and result["pages_followed"] == 0
    assert result["fields"][0] == {"field": "EPSJB", "non_null_rows": 2, "decoded_null_rows": 0}
    assert result["fields"][1] == {"field": "BPS", "non_null_rows": 0, "decoded_null_rows": 2}
    assert len(result["fields"]) == 8 and result["physical_http_attempts"] is None
    assert not result["latest_snapshot_verified"] and not result["full_window_coverage_verified"]
    assert not result["key_lookup_verified"] and not result["revision_index_verified"]
    assert "private-provider-data" not in json.dumps(result)


@pytest.mark.parametrize("component", ("pledge", "unlock"))
def test_structured_ratio_sample_keeps_known_date_discovery_separate(component, monkeypatch):
    monkeypatch.setattr(audit.EastmoneyCompanySource, "pledge_day", lambda *_args: pytest.fail("discovery is outside this sample"))
    day = _NOW.date() if component == "pledge" else _NOW.date() + timedelta(days=1)
    row = {"SECURITY_CODE": "600001", "TRADE_DATE" if component == "pledge" else "FREE_DATE": day.isoformat(),
           "PLEDGE_RATIO" if component == "pledge" else "TOTAL_RATIO": 0 if component == "pledge" else 0.031}
    session = _StructuredPageSession([row])
    result = _page_run(session, StructuredCompanyQuery(component, day, day))
    assert result["status"] == "decoded" and result["decoded_rows"] == 1
    assert len(session.requests) == 1
    assert session.requests[0][1]["params"]["columns"].split(",") == result["requested_fields"]
    assert result["normal_path_discovery"] == ("pledge_day_required" if component == "pledge" else "none_in_current_fetch")
    assert result["fields"][0]["non_null_rows"] == 1


@pytest.mark.parametrize("component,start,end", (
    ("financial", None, date(2026, 11, 5)),
    ("financial", date(2026, 10, 28), date(2026, 11, 5)),
    ("financial", date(2026, 11, 6), date(2026, 11, 6)),
    ("pledge", date(2026, 11, 4), date(2026, 11, 5)),
    ("pledge", date(2025, 11, 4), date(2025, 11, 4)),
    ("unlock", date(2026, 11, 4), date(2026, 11, 5)),
    ("unlock", date(2026, 11, 5), date(2027, 5, 5)),
))
def test_structured_sample_rejects_missing_or_expanded_windows_before_io(component, start, end):
    with pytest.raises(ValueError, match="company_structured_page_window"):
        audit.BuildCompanyStructuredPageQuery(component, start, end, observed_at=_NOW)


def test_structured_sample_expired_deadline_and_continuation_never_fetch():
    session = _StructuredPageSession([])
    query = StructuredCompanyQuery("financial", _NOW.date(), _NOW.date())
    result = _page_run(session, query, monotonic=lambda: 60)
    assert result["status"] == "skipped" and result["fetch_invocations"] == 0 and not session.requests
    with pytest.raises(ValueError, match="options_invalid"):
        _page_run(session, StructuredCompanyQuery("financial", _NOW.date(), _NOW.date(), page=2))
    assert not session.requests


def test_structured_sample_empty_result_does_not_certify_absence_or_trigger_details():
    session = _StructuredPageSession([])
    result = _page_run(session, StructuredCompanyQuery("pledge", _NOW.date(), _NOW.date()))
    assert result["status"] == "empty" and result["raw_rows"] == result["decoded_rows"] == 0
    assert len(session.requests) == 1 and not result["full_window_coverage_verified"]


@pytest.mark.parametrize("rows,total,pages", (([], 1, 1), ([], 501, 2), ([{}], 501, 1), ([{}], 2, 1)))
def test_structured_sample_rejects_contradictory_pagination_without_more_requests(rows, total, pages):
    day = _NOW.date()
    rows = [{"SECURITY_CODE": "600001", "TRADE_DATE": day.isoformat(), "PLEDGE_RATIO": 0} for _ in rows]
    session = _StructuredPageSession(rows, total=total, pages=pages)
    result = _page_run(session, StructuredCompanyQuery("pledge", day, day))
    assert result["status"] == "failed" and result["failure"] == "DownloadValidationFailure"
    assert len(session.requests) == 1


def test_structured_sample_failure_is_bounded_and_never_exposes_remote_exception_text():
    class Session:
        def get(self, *_args, **_kwargs):
            raise HttpConnectionError("private-provider-data")

    result = _page_run(Session(), StructuredCompanyQuery("financial", _NOW.date(), _NOW.date()))
    assert result["status"] == "failed" and result["failure"] == "HttpConnectionError"
    assert result["fetch_invocations"] == 1 and "private-provider-data" not in json.dumps(result)


class _FinancialKeySession:
    def __init__(self):
        self.requests = []
        self.change = None
        self.rows = [{
            "SECUCODE": code + ".SH", "REPORT_DATE": "2026-09-30", "NOTICE_DATE": "2026-10-31",
            **{field: None if field == "BPS" else 0 for _name, field in audit._FINANCIAL_FIELDS},
        } for code in ("600900", "688012")]

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        selected = re.search(r"SECUCODE in \(([^)]+)\)", kwargs["params"]["filter"])
        assert selected is not None
        identities = set(re.findall(r"'([0-9]{6}\.(?:SH|SZ|BJ))'", selected[1]))
        rows = [dict(row) for row in self.rows if row["SECUCODE"] in identities]
        if self.change == "ignored_subset" and len(self.requests) == 2:
            rows = [dict(row) for row in self.rows]
        elif self.change == "missing_key":
            rows = rows[:1]
        elif self.change == "wrong_period":
            rows[0]["REPORT_DATE"] = "2026-06-30"
        elif self.change == "wrong_date":
            rows[0]["NOTICE_DATE"] = "2026-11-01"
        elif self.change == "wrong_exchange":
            rows = [dict(row, SECUCODE=row["SECUCODE"].replace(".SH", ".SZ")) for row in rows]
        elif self.change == "missing_field":
            rows[0].pop("BPS")
        elif self.change == "duplicate_key":
            rows = [rows[0], rows[0]]
        elif self.change == "provider_rejected":
            return HttpResponse(200, b'{"code":1,"success":false,"message":"private-provider-data"}')
        elif self.change == "status_missing":
            return HttpResponse(200, b'{"code":0,"result":null}')
        elif self.change == "bad_root":
            return HttpResponse(200, b'[]')
        elif self.change == "nonstandard_http_success":
            return HttpResponse(201, b'{"success":true,"code":0}')
        elif self.change == "drift" and len(self.requests) == 2:
            rows[0]["EPSJB"] = 1
        return HttpResponse(200, json.dumps({"success": True, "code": 0, "result": {
            "count": len(rows), "pages": 1, "data": rows,
        }}).encode())


def _key_run(session, **kwargs):
    return _page_run(
        session, StructuredCompanyQuery("financial", date(2026, 10, 31), date(2026, 10, 31)),
        financial_codes=("600900", "688012"), financial_report_date=date(2026, 9, 30), **kwargs,
    )


def test_existing_page_mode_checks_batch_keys_and_exclusion_using_the_shared_production_decoder(monkeypatch):
    session = _FinancialKeySession()
    decoded = []
    decoder = audit.DecodeCompanyFinancialPage

    def decode(*args, **kwargs):
        decoded.append(True)
        return decoder(*args, **kwargs)

    monkeypatch.setattr(audit, "DecodeCompanyFinancialPage", decode)
    monkeypatch.setattr(audit.EastmoneyCompanySource, "fetch", lambda *_args, **_kwargs: pytest.fail("normal source request must stay unchanged"))
    result = _key_run(session)
    assert result["status"] == "decoded" and len(decoded) == len(session.requests) == result["fetch_invocations"] == 2
    assert result["key_filter_requested"] and result["request_limit"] == result["page_limit"] == result["page_size"] == 2
    assert result["request_style"] == "APP_F10_MAINFINADATA" and not result["remote_column_projection_requested"]
    assert result["requested_fields"] == []
    assert result["required_fields"] == ["SECUCODE", "REPORT_DATE", "NOTICE_DATE", *(field for _name, field in audit._FINANCIAL_FIELDS)]
    assert result["key_filter_probe"]["support_status"] == "sample_verified" and result["key_filter_probe"]["filter_verified"]
    assert not result["key_filter_probe"]["date_filter_independently_verified"]
    assert not result["key_lookup_verified"] and not result["latest_snapshot_verified"] and not result["revision_index_verified"]
    for index, (_url, request) in enumerate(session.requests):
        parameters = request["params"]
        assert parameters["sty"] == "APP_F10_MAINFINADATA"
        assert "NOTICE_DATE>='2026-10-31'" in parameters["filter"] and "NOTICE_DATE<='2026-10-31'" in parameters["filter"]
        assert "REPORT_DATE='2026-09-30'" in parameters["filter"] and "ISNEW" not in parameters["filter"]
        assert parameters["p"] == "1" and parameters["ps"] == str(2 - index)
        assert request["max_response_bytes"] == 1024 * 1024 and request["deadline"] == 60
    assert result["fields"][1]["decoded_null_rows"] == 2


@pytest.mark.parametrize("change,expected,requests", (
    ("ignored_subset", "company_structured_key_pagination_invalid", 2),
    ("missing_key", "company_structured_key_set_mismatch", 1),
    ("wrong_period", "company_structured_key_set_mismatch", 1),
    ("wrong_date", "DownloadValidationFailure", 1),
    ("wrong_exchange", "company_structured_key_raw_identity_mismatch", 1),
    ("missing_field", "company_structured_key_fields_missing", 1),
    ("duplicate_key", "company_structured_key_identity_ambiguous", 1),
    ("provider_rejected", "company_structured_key_provider_rejected", 1),
    ("status_missing", "company_structured_key_provider_status_invalid", 1),
    ("bad_root", "company_structured_key_response_schema_invalid", 1),
    ("nonstandard_http_success", "company_structured_key_http_status_invalid", 1),
    ("drift", "company_structured_key_source_changed", 2),
))
def test_key_filter_mismatch_drift_and_missing_data_never_trigger_unbounded_fallback(change, expected, requests):
    session = _FinancialKeySession()
    session.change = change
    result = _key_run(session)
    assert result["status"] == "failed" and result["failure"] == expected
    assert len(session.requests) == result["fetch_invocations"] == requests
    assert result["key_filter_probe"]["support_status"] == "not_verified" and not result["key_filter_probe"]["filter_verified"]
    assert "private-provider-data" not in json.dumps(result)


def test_key_filter_deadline_stops_before_the_second_request():
    session = _FinancialKeySession()
    ticks = iter((0, 0, 0, 0, 60, 60))
    result = _key_run(session, monotonic=lambda: next(ticks, 60))
    assert result["status"] == "failed" and result["failure"] == "company_structured_key_deadline_exhausted"
    assert result["fetch_invocations"] == len(session.requests) == 1


def test_key_filter_binds_the_full_beijing_exchange_identity_before_certifying_the_sample():
    session = _FinancialKeySession()
    codes = ("920001", "830001")
    session.rows = [dict(row, SECUCODE=code + ".BJ") for row, code in zip(session.rows, codes, strict=False)]
    result = _page_run(
        session, StructuredCompanyQuery("financial", date(2026, 10, 31), date(2026, 10, 31)),
        financial_codes=codes, financial_report_date=date(2026, 9, 30),
    )
    assert result["status"] == "decoded" and result["key_filter_probe"]["filter_verified"]
    assert len(session.requests) == 2
    assert "'920001.BJ','830001.BJ'" in session.requests[0][1]["params"]["filter"]
    assert "'920001.BJ'" in session.requests[1][1]["params"]["filter"]
