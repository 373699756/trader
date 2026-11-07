from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import date, datetime
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import pytest

from scripts.download.history.cli import BuildParser, ParseOptions
from scripts.download.history.cninfo_industry_probe import CninfoIndustryProbeRequest, ProbeCninfoIndustryQueries
from scripts.download.history.commands import BuildCommands
from trader.infra.http_contracts import HttpRequest, HttpResponse
from trader.infra.http_transport import RequestsHttpTransport

_AT = datetime(2026, 11, 7, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
_CODES = ("002831", "002832")
_FROM = date(2024, 1, 1)
_TO = date(2026, 11, 7)


def _row(code, **values):
    return {"SECCODE": code, "VARYDATE": "2024-09-30", "F001V": "008001", "F007V": None, **values}


def _response(records, **values):
    return HttpResponse(200, json.dumps({"resultcode": 200, "count": len(records), "records": records, **values}).encode())


def _parameters(request):
    return parse_qs(request.body.decode() if request.body is not None else urlsplit(request.url).query)


def _run(root, execute=None, *, codes=_CODES, cancel_requested=lambda: False, monotonic_clock=lambda: 100.0):
    calls = []

    def dispatch(request: HttpRequest):
        calls.append(request)
        if execute is not None:
            return execute(request)
        codes = _parameters(request)["scode"][0].split(",")
        return _response([_row(code) for code in reversed(codes)])

    with RequestsHttpTransport(execute_request=dispatch, monotonic=monotonic_clock) as transport:
        result = ProbeCninfoIndustryQueries(
            CninfoIndustryProbeRequest(codes, _FROM, _TO, root, 10, 60), transport,
            clock=lambda: _AT, monotonic_clock=monotonic_clock, cancel_requested=cancel_requested,
        )
    return result, calls


def test_three_encodings_compare_all_fields_preserve_originals_and_never_publish(tmp_path):
    report, calls = _run(tmp_path)
    assert report["status"] == "degraded"
    summary = report["summary"]
    assert summary["baseline_status"] == "compared"
    assert summary["network_requests"] == len(calls) == 5
    assert summary["production_eligible"] is False and summary["source_data_writes"] == 0
    assert summary["window_coverage_verified"] is False
    assert [item.method for item in calls] == ["GET", "POST", "POST", "POST", "POST"]
    assert calls[0].body is None and calls[1].body is None and calls[2].body is not None
    assert all(_parameters(item)["sdate"] == ["2024-01-01"] for item in calls)
    assert all(item.deadline == 110 for item in calls)
    assert all(item.max_response_bytes == 2 * 1024 * 1024 for item in calls)
    assert all(item["baseline_comparison"] == "equal" for item in summary["batch_queries"])
    assert all(item["complete_against_baseline"] is True for item in summary["batch_queries"])
    assert all(item["actual_codes"] == _CODES for item in summary["batch_queries"])
    assert all(item["null_fields"] == 2 for item in summary["batch_queries"])
    for item in summary["batch_queries"]:
        receipt = json.loads((tmp_path / (item["query"] + ".receipt.json")).read_text())
        original = (tmp_path / (receipt["sha256"] + ".blob")).read_bytes()
        assert hashlib.sha256(original).hexdigest() == item["response_sha256"]
        assert receipt["received_at"] == item["received_at"] == _AT.isoformat()
        assert receipt["codes"] == list(_CODES)
        assert "headers" not in receipt and "Accept-Enckey" not in json.dumps(receipt)


def test_successful_empty_batch_is_not_complete_when_single_security_has_records(tmp_path):
    def execute(request):
        codes = _parameters(request)["scode"][0].split(",")
        return _response([] if len(codes) > 1 else [_row(codes[0])])

    report, _calls = _run(tmp_path, execute)
    assert report["status"] == "failed"
    assert all(item["status"] == "empty" for item in report["summary"]["batch_queries"])
    assert all(item["baseline_comparison"] == "different" for item in report["summary"]["batch_queries"])
    assert not any(item["complete_against_baseline"] for item in report["summary"]["batch_queries"])


def test_record_field_difference_is_not_hidden_by_matching_codes_and_counts(tmp_path):
    def execute(request):
        codes = _parameters(request)["scode"][0].split(",")
        return _response([_row(code, F007V="changed" if request.method == "GET" else None) for code in codes])

    report, _calls = _run(tmp_path, execute)
    assert report["status"] == "failed"
    assert report["summary"]["batch_queries"][0]["baseline_comparison"] == "different"
    assert report["summary"]["batch_queries"][1]["baseline_comparison"] == "equal"


@pytest.mark.parametrize("response,failure", [
    (_response([_row("002831")], count=2), "count_mismatch"),
    (_response([_row("002831")], total=2), "truncated"),
    (_response([_row("002831")], total=True), "total_invalid"),
    (_response([], resultcode=402), "query_rejected"),
    (_response([_row("999999")]), "code_identity_invalid"),
    (HttpResponse(200, b"bad-json"), "invalid_json"),
])
def test_invalid_or_truncated_response_is_reported_and_original_bytes_are_retained(tmp_path, response, failure):
    report, _calls = _run(tmp_path, lambda request: response)
    assert report["status"] == "failed"
    assert report["summary"]["batch_queries"][0]["failure"] == failure
    assert report["summary"]["batch_queries"][0]["empty"] is None
    assert report["summary"]["batch_queries"][0]["missing_codes"] == ()
    digest = hashlib.sha256(response.content).hexdigest()
    assert (tmp_path / (digest + ".blob")).read_bytes() == response.content


def test_six_codes_skip_single_baselines_without_claiming_complete(tmp_path):
    codes = tuple(f"{value:06d}" for value in range(1, 7))
    report, calls = _run(tmp_path, codes=codes)
    assert len(calls) == 3 and report["summary"]["baseline_status"] == "not_compared"
    assert report["summary"]["single_queries"] == []
    assert all(item["complete_against_baseline"] is False for item in report["summary"]["batch_queries"])


def test_cancellation_prevents_all_network_and_writes_no_response(tmp_path):
    report, calls = _run(tmp_path, cancel_requested=lambda: True)
    assert calls == [] and report["status"] == "failed"
    assert report["summary"]["network_requests"] == 0
    assert not list(tmp_path.glob("*.blob"))


def test_expired_shared_deadline_prevents_remaining_physical_requests(tmp_path):
    current = [100.0]

    def execute(request):
        current[0] = 200.0
        return _response([])

    report, calls = _run(tmp_path, execute, monotonic_clock=lambda: current[0])
    assert report["status"] == "failed" and len(calls) == 1
    assert report["summary"]["network_requests"] == 1
    assert report["summary"]["elapsed_seconds"] == 100
    assert not list(tmp_path.glob("*.blob"))


def test_server_row_cap_is_not_reported_as_complete_even_without_total(tmp_path):
    report, _calls = _run(tmp_path, lambda request: _response([_row("002831")] * 20_000))
    assert report["status"] == "failed"
    assert report["summary"]["batch_queries"][0]["truncated"] is True
    assert report["summary"]["batch_queries"][0]["failure"] == "truncated"


def test_probe_refuses_existing_evidence_directory_without_requery_or_overwrite(tmp_path):
    before, _calls = _run(tmp_path)
    evidence = {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.suffix != ".lock"}
    with pytest.raises(ValueError, match="empty_directory"):
        _run(tmp_path)
    assert before["status"] == "degraded"
    assert evidence == {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.suffix != ".lock"}


def _options(arguments):
    return ParseOptions(BuildParser("history-industry").parse_args(arguments))


def test_probe_cli_builds_only_the_existing_industry_worker_with_explicit_inputs(tmp_path):
    options = _options([
        "--cninfo-query-codes", *_CODES, "--cninfo-query-from", "2024-01-01", "--cninfo-query-to", "2026-11-07",
        "--industry-work-root", str(tmp_path),
    ])
    command, = BuildCommands(options)
    assert command.name == "history_industry"
    assert "scripts.download.history.history_industry_preparation" in command.argv
    assert "--cninfo-query-codes" in command.argv and "--official-announcements" not in command.argv
    assert options.cninfo_query_from == _FROM and options.cninfo_query_to == _TO


def test_worker_selects_probe_with_injected_transport_and_closes_it(tmp_path, monkeypatch):
    import sys

    import trader.bootstrap as bootstrap
    from scripts.download.history import history_industry_preparation as worker

    transport = RequestsHttpTransport(execute_request=lambda request: pytest.fail("worker fixture must not request HTTP"))
    factory_arguments = []
    reports = []
    requests = []

    def factory(**arguments):
        factory_arguments.append(arguments)
        return transport

    def probe(request, borrowed):
        requests.append(request)
        assert borrowed is transport
        return {"status": "degraded", "summary": {"production_eligible": False}}

    monkeypatch.setattr(bootstrap, "OpenExternalHttp", factory)
    monkeypatch.setattr(worker, "ProbeCninfoIndustryQueries", probe)
    monkeypatch.setattr(worker, "AuditOfficialIndustrySources", lambda *args: pytest.fail("wrong source selected"))
    monkeypatch.setattr(worker, "emit_report", reports.append)
    monkeypatch.setattr(sys, "argv", [
        "industry", "--cninfo-query-codes", *_CODES, "--cninfo-query-from", "2024-01-01",
        "--cninfo-query-to", "2026-11-07", "--industry-work-root", str(tmp_path),
    ])
    assert worker.Main() == 0
    assert requests[0].codes == _CODES and requests[0].from_date == _FROM and requests[0].to_date == _TO
    assert factory_arguments == [{"identity_content_encodings": ("normal",)}]
    assert reports[0]["status"] == "degraded" and transport.is_closed()


@pytest.mark.parametrize("codes", [("002831",), ("002832", "002831"), ("002831", "002831"), ("002831", "invalid")])
def test_probe_cli_rejects_invalid_request_membership_before_execution(tmp_path, codes):
    with pytest.raises(ValueError, match="CNINFO probe"):
        _options([
            "--cninfo-query-codes", *codes, "--cninfo-query-from", "2024-01-01", "--cninfo-query-to", "2026-11-07",
            "--industry-work-root", str(tmp_path),
        ])


def test_official_source_mode_remains_explicit_and_excludes_cninfo_options(tmp_path):
    options = _options(["--official-announcements", "https://www.capco.org.cn/dataFile/zsxfb/hyfl/hyfljg/fixture.js", "--industry-work-root", str(tmp_path)])
    assert options.cninfo_query_codes == ()
    assert "--official-announcements" in BuildCommands(options)[0].argv
    with pytest.raises(SystemExit):
        _options(["--official-announcements", "https://www.capco.org.cn/dataFile/zsxfb/hyfl/hyfljg/fixture.js", "--cninfo-query-codes", *_CODES])


def test_probe_validation_rejects_missing_dates_before_transport_or_directory_creation(tmp_path):
    request = replace(CninfoIndustryProbeRequest(_CODES, _FROM, _TO, tmp_path), from_date=None)
    with RequestsHttpTransport(execute_request=lambda request: pytest.fail("invalid request must not send HTTP")) as transport:
        with pytest.raises(ValueError, match="request_invalid"):
            ProbeCninfoIndustryQueries(request, transport, clock=lambda: _AT)
