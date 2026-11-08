"""Read-only fixtures for name-history speed and correctness evidence."""

import json
import time
from collections import Counter
from contextlib import nullcontext
from datetime import datetime
from threading import Barrier, Event, Lock
from zoneinfo import ZoneInfo

import pytest

from scripts.download.company.company_st_source_audit import AuditCompanyStSources, CompanyStSourceRequest
from trader.infra.http_contracts import HttpConnectionError, HttpResponse

_NOW = datetime(2026, 11, 8, 12, tzinfo=ZoneInfo("Asia/Shanghai"))


def _json_response(code="600848", current="上海临港", former="自仪股份→ST自仪→SST自仪", **changes):
    row = {"SECURITY_CODE": code, "SECURITY_NAME_ABBR": current, "STR_NAMEA": current, "FORMERNAME": former,
           "CONTACT_TEL": "PRIVATE_CONTACT", "ORG_PROFILE": "PRIVATE_FULL_PAYLOAD"}
    row.update(changes)
    return HttpResponse(200, json.dumps({"jbzl": [row], "fxxg": [{"CONTACT": "PRIVATE_CONTACT"}]}).encode())


def _sina_response(former="自仪股份 ST自仪 SST自仪"):
    return HttpResponse(200, ("<td>证券简称更名历史：</td><td>" + former + "</td>").encode("gb18030"))


class _Clock:
    def __init__(self):
        self.value = 100.0

    def now(self):
        return self.value


class _Transport:
    def __init__(self, *, candidate=None, baseline=None, clock=None, durations=()):
        self.candidate = candidate
        self.baseline = baseline or _sina_response()
        self.clock = clock
        self.durations = list(durations)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.clock is not None:
            self.clock.value += self.durations.pop(0) if self.durations else 0.1
        if "/CompanySurvey/PageAjax" in url:
            response = self.candidate or _json_response()
        else:
            response = self.baseline
        return response(url, kwargs) if callable(response) else response


def _audit(transport, requests=None, *, clock=None, **options):
    clock = clock or _Clock()
    return AuditCompanyStSources(requests or (CompanyStSourceRequest("600848", "上海临港", "ever_st"),),
        session_factory=lambda: nullcontext(transport), rounds=options.pop("rounds", 3),
        workers=options.pop("workers", 1), deadline=options.pop("deadline", 1000.0),
        monotonic=clock.now, clock=lambda: _NOW, **options)


def test_audit_counts_exact_single_attempts_and_projects_only_safe_fields():
    clock = _Clock()
    transport = _Transport(clock=clock)
    report = _audit(transport, clock=clock)
    assert report["probe"] == "st_name_history" and "schema_version" not in report
    assert report["actual_requests"] == report["planned_requests"] == report["request_limit"] == 4
    assert Counter("candidate" if "PageAjax" in call[0] else "sina" for call in transport.calls) == {"candidate": 3, "sina": 1}
    assert all(call[1]["timeout"] <= 8 and call[1]["deadline"] == 1000 for call in transport.calls)
    assert all(call[1]["max_response_bytes"] == 1024 * 1024 for call in transport.calls)
    assert all(call[1]["cancel_requested"]() is False for call in transport.calls)
    assert report["status"] == "observed" and report["source_conflicts"] == 0
    assert report["source_data_writes"] == 0 and report["read_only"] is True
    assert report["production_adopted"] is report["field_correctness_verified"] is report["full_market_verified"] is False
    assert "PRIVATE_CONTACT" not in json.dumps(report) and "PRIVATE_FULL_PAYLOAD" not in json.dumps(report)
    assert "CONTACT_TEL" not in json.dumps(report) and "ORG_PROFILE" not in json.dumps(report)
    candidate, sina = report["sources"]
    assert candidate["actual_requests"] == 3 and sina["actual_requests"] == 1
    assert candidate["success_rate"] == 1 and candidate["known_positive_misses"] == 0
    assert all(item["historical_positive_found"] == [True, True, True] for item in report["comparisons"])


@pytest.mark.parametrize(("changes", "reason"), (
    ({"SECURITY_CODE": "600001"}, "eastmoney_security_code_mismatch"),
    ({"SECURITY_NAME_ABBR": "别的股票"}, "eastmoney_current_name_mismatch"),
    ({"STR_NAMEA": "别的股票"}, "eastmoney_current_name_mismatch"),
    ({"FORMERNAME": None}, "eastmoney_former_names_missing_or_invalid"),
    ({"FORMERNAME": ["ST自仪"]}, "eastmoney_former_names_missing_or_invalid"),
))
def test_candidate_rejects_mismatched_identity_and_nonstring_history(changes, reason):
    transport = _Transport(candidate=_json_response(**changes))
    report = _audit(transport, rounds=1)
    candidate = report["observations"][0]
    assert candidate["attempted"] and candidate["status"] == "failed" and candidate["eligibility"] == "unknown"
    assert candidate["reason"] == reason and candidate["expected_match"] is None
    assert report["actual_requests"] == 2 and report["status"] == "degraded"


def test_name_mismatch_retains_safe_actual_identity_without_changing_strict_result():
    request = CompanyStSourceRequest("001381", "C皇冠新材", "clear")
    report = _audit(_Transport(candidate=_json_response(code="001381", current="皇冠新材", former="")),
        (request,), rounds=1)
    candidate = report["observations"][0]
    assert candidate["current_name"] == "C皇冠新材" and candidate["status"] == "failed"
    assert candidate["reason"] == "eastmoney_current_name_mismatch" and candidate["eligibility"] == "unknown"
    assert candidate["structure"]["observed_code"] == "001381"
    assert candidate["structure"]["observed_current_name"] == candidate["structure"]["observed_a_name"] == "皇冠新材"
    assert report["sources"][0]["usable_results"] == 0 and report["sources"][0]["unknown_results"] == 1


def test_actual_identity_projection_never_exposes_invalid_code_or_unbounded_name():
    report = _audit(_Transport(candidate=_json_response(code="PRIVATE_CODE", current="公" * 41, STR_NAMEA="https://private.invalid/token")), rounds=1)
    structure = report["observations"][0]["structure"]
    assert structure["observed_code"] is None and structure["observed_current_name"] is None
    assert structure["observed_a_name"] == "[省略]"
    assert "PRIVATE_CODE" not in json.dumps(report) and "private.invalid" not in json.dumps(report)


@pytest.mark.parametrize("payload", ({}, {"jbzl": []}, {"jbzl": [{}, {}]}, {"jbzl": "not-list"}))
def test_candidate_requires_exactly_one_company_record(payload):
    report = _audit(_Transport(candidate=HttpResponse(200, json.dumps(payload).encode())), rounds=1)
    assert report["observations"][0]["reason"] == "eastmoney_name_history_rows_invalid"


@pytest.mark.parametrize("former", ("", "-", "--", "暂无", "暂无数据", "－－"))
def test_empty_former_names_never_qualify_as_clear(former):
    report = _audit(_Transport(candidate=_json_response(former=former)), rounds=1)
    candidate = report["observations"][0]
    assert candidate["status"] == "unknown_unverified" and candidate["eligibility"] == "unknown"
    assert candidate["empty_history"] is True and candidate["expected_match"] is None
    assert report["sources"][0]["known_positive_unknown"] == 1 and report["sources"][0]["success_rate"] == 0


def test_current_st_name_is_only_positive_context_when_history_is_empty():
    request = CompanyStSourceRequest("600848", "*ST公司", "ever_st")
    report = _audit(_Transport(candidate=_json_response(current="*ST公司", former="")), (request,), rounds=1)
    assert report["observations"][0]["current_name_is_st"] is True
    assert report["observations"][0]["eligibility"] == "unknown"


def test_current_st_name_does_not_prove_the_history_field_found_a_past_st_name():
    request = CompanyStSourceRequest("600848", "*ST公司", "ever_st")
    report = _audit(_Transport(candidate=_json_response(current="*ST公司", former="正常公司")), (request,), rounds=1)
    candidate = report["observations"][0]
    assert candidate["eligibility"] == "ever_st" and candidate["current_name_is_st"] is True
    assert candidate["history_is_st"] is False and candidate["expected_match"] is None
    assert report["sources"][0]["historical_positive_hits"] == 0
    assert report["sources"][0]["historical_positive_unverified"] == 1 and report["status"] == "degraded"


def test_historical_positive_misses_and_source_conflicts_are_reported_per_round():
    report = _audit(_Transport(candidate=_json_response(former="自仪股份")))
    candidate = report["sources"][0]
    assert candidate["known_positive_misses"] == 3 and candidate["confusion"]["ever_st"]["clear"] == 3
    assert report["source_conflicts"] == 3 and report["comparisons"][0]["expected_matches"] == [False] * 3
    assert report["status"] == "degraded" and report["field_correctness_verified"] is False


def test_cross_round_drift_and_latency_quantiles_retain_real_observations():
    clock, candidates = _Clock(), []

    def candidate(url, kwargs):
        candidates.append(url)
        return _json_response(former="ST自仪" if len(candidates) == 1 else "自仪股份")

    report = _audit(_Transport(candidate=candidate, clock=clock, durations=(0.1, 0.2, 0.5, 1.0)), clock=clock)
    source = report["sources"][0]
    assert source["p50_seconds"] == pytest.approx(0.5)
    assert source["p95_seconds"] == pytest.approx(0.95) and source["max_seconds"] == pytest.approx(1.0)
    assert report["eligibility_drift_codes"] == report["observation_drift_codes"] == ["600848"]
    assert report["comparisons"][0]["historical_positive_found"] == [True, False, False]


@pytest.mark.parametrize(("first", "second"), (
    ("前身甲公司", "前身乙公司"),
    ("正常公司" * 60 + "甲", "正常公司" * 60 + "乙"),
))
def test_full_history_hash_detects_drift_even_when_status_and_excerpt_do_not(first, second):
    seen = []

    def candidate(url, kwargs):
        seen.append(url)
        return _json_response(former=first if len(seen) == 1 else second)

    request = CompanyStSourceRequest("600848", "上海临港", "clear")
    report = _audit(_Transport(candidate=candidate, baseline=_sina_response("正常公司")), (request,), rounds=2)
    observations = [item for item in report["observations"] if item["source"] == "eastmoney_f10"]
    assert all(item["eligibility"] == "clear" for item in observations)
    assert report["eligibility_drift_codes"] == [] and report["observation_drift_codes"] == ["600848"]
    assert observations[0]["former_names_sha256"] != observations[1]["former_names_sha256"]
    assert all(len(item["former_names_sha256"]) == 64 for item in observations)
    if len(first) > 160:
        assert observations[0]["former_names_excerpt"] == observations[1]["former_names_excerpt"]


def test_history_digest_normalizes_full_width_and_whitespace_without_false_drift():
    seen = []

    def candidate(url, kwargs):
        seen.append(url)
        return _json_response(former="前身Ａ公司\n  前身Ｂ公司" if len(seen) == 1 else "前身A公司 前身B公司")

    request = CompanyStSourceRequest("600848", "上海临港", "clear")
    report = _audit(_Transport(candidate=candidate, baseline=_sina_response("正常公司")), (request,), rounds=2)
    observations = [item for item in report["observations"] if item["source"] == "eastmoney_f10"]
    assert observations[0]["former_names_sha256"] == observations[1]["former_names_sha256"]
    assert report["observation_drift_codes"] == []


@pytest.mark.parametrize("former", ("", "--"))
def test_empty_sina_history_is_unverified_and_preserves_original_parser_result(former):
    report = _audit(_Transport(baseline=_sina_response(former)), rounds=1)
    sina = report["observations"][1]
    assert sina["status"] == "unknown_unverified" and sina["eligibility"] == "unknown"
    assert sina["original_parser_status"] == ("clear" if former == "" else None)
    assert report["source_conflicts"] == 0


def test_sina_comparison_uses_first_history_cell_and_the_actual_production_parser():
    text = ("<td>证券简称更名历史：</td><td>FAST<span>ST自仪</span>自仪</td>"
            "<td>证券简称更名历史：</td><td>正常公司</td>")
    report = _audit(_Transport(baseline=HttpResponse(200, text.encode("gb18030"))), rounds=1)
    sina = report["observations"][1]
    assert sina["eligibility"] == sina["original_parser_status"] == "ever_st"
    assert sina["history_is_st"] is True and report["source_conflicts"] == 0


@pytest.mark.parametrize("stop", ("deadline", "cancelled"))
def test_audit_before_stop_does_not_count_or_dispatch_requests(stop):
    cancelled = Event()
    if stop == "cancelled":
        cancelled.set()
    transport = _Transport()
    report = _audit(transport, deadline=100.0 if stop == "deadline" else 1000.0, cancel_requested=cancelled.is_set)
    assert report["actual_requests"] == 0 and transport.calls == []
    assert report["status"] == ("timed_out" if stop == "deadline" else "cancelled")
    assert all(row["status"] == "not_requested" and not row["attempted"] for row in report["observations"])
    assert all(source["failed_requests"] == 0 and source["success_rate"] is None for source in report["sources"])


@pytest.mark.parametrize("stop", ("deadline", "cancelled"))
def test_late_response_is_not_accepted_and_remaining_jobs_are_not_dispatched(stop):
    clock, cancelled = _Clock(), Event()

    def candidate(url, kwargs):
        if stop == "cancelled":
            cancelled.set()
        else:
            clock.value = 102.0
        return _json_response()

    transport = _Transport(candidate=candidate)
    report = _audit(transport, clock=clock, deadline=101.0, cancel_requested=cancelled.is_set)
    assert report["actual_requests"] == len(transport.calls) == 1
    assert report["observations"][0]["eligibility"] == "unknown"
    assert all(row["status"] == "not_requested" for row in report["observations"][1:])
    assert transport.calls[0][1]["timeout"] == 1.0


def test_http_failure_is_not_retried_and_cause_projection_excludes_payloads():
    def failure(url, kwargs):
        raise HttpConnectionError("PRIVATE_ERROR https://private.invalid/token") from OSError("PRIVATE_CAUSE")

    transport = _Transport(candidate=failure)
    report = _audit(transport, rounds=1)
    assert report["actual_requests"] == len(transport.calls) == 2
    observation = report["observations"][0]
    assert "HttpConnectionError" in observation["diagnostic"] and "OSError" in observation["diagnostic"]
    assert "PRIVATE_ERROR" not in json.dumps(report) and "PRIVATE_CAUSE" not in json.dumps(report)
    assert "private.invalid" not in json.dumps(report)


def test_arbitrary_lowercase_exception_message_is_not_a_public_reason():
    def failure(url, kwargs):
        raise OSError("private_token_123")

    report = _audit(_Transport(candidate=failure), rounds=1)
    assert report["observations"][0]["reason"] == "source_io_failed"
    assert "private_token_123" not in json.dumps(report)


def test_audit_rejects_body_over_the_transport_limit_without_decoding():
    report = _audit(_Transport(candidate=HttpResponse(200, b"x" * (1024 * 1024 + 1))), rounds=1)
    assert report["observations"][0]["status"] == "failed"
    assert report["observations"][0]["reason"] == "http_response_too_large"


def test_bounded_parallel_workers_use_only_two_simultaneous_calls():
    barrier, lock = Barrier(2), Lock()
    active, peak = 0, 0

    class Transport:
        def get(self, url, **kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                barrier.wait(timeout=3)
                return _json_response(code="600001", current="正常公司", former="正常公司") if "PageAjax" in url else _sina_response("正常公司")
            finally:
                with lock:
                    active -= 1

    report = AuditCompanyStSources((CompanyStSourceRequest("600001", "正常公司", "clear"),),
        session_factory=lambda: nullcontext(Transport()), rounds=1, workers=2,
        timeout_seconds=1, deadline=time.monotonic() + 5, clock=lambda: _NOW)
    assert peak == 2 and active == 0 and report["actual_requests"] == 2 and report["status"] == "observed"


@pytest.mark.parametrize("options", ({"rounds": 0}, {"rounds": 6}, {"workers": 0}, {"workers": 5}, {"timeout_seconds": 16}))
def test_invalid_budgets_are_rejected_before_factory_use(options):
    with pytest.raises(ValueError, match="options_invalid"):
        _audit(_Transport(), **options)


def test_duplicate_population_and_more_than_sixty_four_codes_are_rejected():
    request = CompanyStSourceRequest("600848", "上海临港", "unknown")
    for requests in ((request, request), tuple(CompanyStSourceRequest(f"600{number:03d}", "正常公司", "unknown") for number in range(65))):
        with pytest.raises(ValueError, match="options_invalid"):
            _audit(_Transport(), requests)


def test_local_clear_reference_is_not_promoted_to_absolute_truth():
    request = CompanyStSourceRequest("600848", "上海临港", "clear")
    report = _audit(_Transport(), (request,), rounds=1)
    assert report["sources"][0]["local_clear_conflicts"] == 1
    assert report["expectation_role"] == "local_reference_not_absolute_truth"
    assert report["field_correctness_verified"] is False and report["status"] == "degraded"
