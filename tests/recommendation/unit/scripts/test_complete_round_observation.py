"""Live observation counts published SSE identities, never retained ready snapshots."""

from __future__ import annotations

import copy
import json
import os
import sys
import threading
import time
import zlib
from dataclasses import replace
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

import pytest

from scripts.recommendation.web.complete_round_observation import ObservationFetchError, ObserveCompleteRounds
from scripts.core.process import RunBoundedCommand
from scripts.core.catalog import WorkerArguments
from trader.infra.http_contracts import HttpTimeout
from trader.infra.http_transport import RequestsHttpTransport
from trader.recommendation.domain.evidence.pipeline import PIPELINE_STAGES

_START = datetime(2026, 11, 6, 13, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
_PROFILES = ("v1", "v2", "v3", "v5")


def _pipeline():
    audit = [{
        "stage": stage.value, "stage_order": index + 1, "state": "ready",
        "input_count": 6, "output_count": 6, "rejected_count": 0, "pending_count": 0,
        "failed_count": 0, "not_selected_count": 0, "input_batch_id": f"batch:{index}",
        "output_batch_id": f"batch:{index+1}", "as_of": _START.isoformat(), "latency_ms": index,
        "operation_durations": [{"operation": "fixture_operation", "duration_ms": index}],
    } for index, stage in enumerate(PIPELINE_STAGES)]
    return {
        "current_stage": "action_publish", "stage_snapshots": audit,
        "stages": [{"key": stage.value, "state": "completed", "input_count": 6, "output_count": 6, "facets": []}
                   for stage in PIPELINE_STAGES],
    }


def _batch(sequence, *, elapsed=15, source_age=2, generation=1, mutation=None):
    observed = _START + timedelta(seconds=elapsed)
    sources = observed - timedelta(seconds=source_age)
    event = {"schema_version": "freeze_event", "round_sequence": sequence, "profiles": {}}
    atomic = {"schema_version": "freeze_current_batch", "profiles": {}}
    for profile in _PROFILES:
        event["profiles"][profile] = {}
        atomic["profiles"][profile] = {}
        for strategy in ("tomorrow", "d25"):
            identity = f"publication:{sequence}:{profile}:{strategy}"
            common = {
                "scoring_profile": profile, "strategy": strategy, "stage": "local", "frozen": False,
                "trade_date": observed.date().isoformat(), "projection_version": identity,
                "input_versions": {"native": f"native:{sequence}", "session_generation": str(generation)},
                "coverage": {"selected_count": 6, "evaluated_count": 6}, "pipeline": _pipeline(),
            }
            flat = [{
                "code": f"60000{index}", "rank": index + 1, "selected": True,
                "source_time": sources.isoformat(), "price": 123.45,
            } for index in range(6)]
            event["profiles"][profile][strategy] = {
                **copy.deepcopy(common), "version": identity, "published_at": observed.isoformat(), "upserts": flat,
            }
            atomic["profiles"][profile][strategy] = {
                **copy.deepcopy(common), "freeze_version": identity, "observed_at": observed.isoformat(), "status": "ready",
                "items": [{"code": item["code"], "rank": item["rank"], "selected": True,
                           "quote": {"source_time": item["source_time"], "price": item["price"]}} for item in flat],
            }
    if mutation is not None:
        mutation(event, atomic)
    return event, atomic


def _frame(payload, *, event_id=11, event_type="batch"):
    return f"id: {event_id}\nevent: {event_type}\ndata: {json.dumps(payload,ensure_ascii=False)}\n\n".encode()


class _Fixture:
    def __init__(self, rows, *, duration=100, initial_cursor=10, fetch_error=None, runtime_changed=False):
        self.rows = rows
        self.elapsed = 0.0
        self.initial_cursor = initial_cursor
        self.atomic = _batch(0, elapsed=0)[1]
        self.current_sequence = 0
        self.requests = []
        self.duration = duration
        self.fetch_error = fetch_error
        self.runtime_changed = runtime_changed
        self.stream_request = None
        self.polls = 0

    def fetch(self, url, timeout):
        self.requests.append((url, self.elapsed, timeout))
        if url.endswith("/api/freezes/current"):
            if self.fetch_error is not None:
                raise ObservationFetchError(self.fetch_error)
            return self.atomic
        self.polls += 1
        return {
            "runtime_version": "changed-runtime" if self.runtime_changed and self.polls > 1 else "fixture-runtime",
            "available_profiles": list(_PROFILES), "phase": "before_close",
            "events": {"sequence": self.initial_cursor + self.current_sequence},
            "scheduler": {"recommendation_round": {
                "sequence": self.current_sequence, "outcome": "published", "elapsed_ms": 1200,
            }},
        }

    def execute(self, request):
        self.stream_request = request
        request.receive_headers(200, (("Content-Type", "text/event-stream; charset=utf-8"),))
        request.receive_chunk(b": connected\n\n")
        for elapsed, event, atomic, event_id, event_type in self.rows:
            self.elapsed = elapsed
            self.atomic = atomic
            self.current_sequence = event.get("round_sequence", 0)
            request.receive_chunk(_frame(event, event_id=event_id, event_type=event_type))
        self.elapsed = self.duration
        raise HttpTimeout("fixture_timeout")

    def run(self, *, required=1):
        return ObserveCompleteRounds(
            "http://localhost", required_rounds=required, observation_seconds=self.duration,
            interval_seconds=15, timeout_seconds=3, scoring_profile="v2", fetch_mapping=self.fetch,
            transport=self, monotonic=lambda: self.elapsed, wall_clock=lambda: _START + timedelta(seconds=self.elapsed),
        )


def test_complete_round_mode_counts_five_new_atomic_eight_head_rounds_and_keeps_sanitized_evidence():
    rows = []
    for index in range(1, 6):
        event, atomic = _batch(index, elapsed=index * 15)
        rows.append((index * 15, event, atomic, index + 10, "batch"))
    fixture = _Fixture(rows)
    report = fixture.run(required=5)
    assert report["status"] == "passed"
    assert report["summary"]["stop_reason"] == "target_reached"
    assert report["summary"]["observed_batch_events"] == 5
    assert report["summary"]["complete_published_rounds"] == 5
    assert report["summary"]["identity_matched_rounds"] == report["summary"]["source_age_qualified_rounds"] == 5
    assert report["summary"]["current_consecutive_qualified_rounds"] == 5
    assert report["summary"]["maximum_consecutive_qualified_rounds"] == 5
    assert all(len(row["heads"]) == len(row["published_heads"]) == len(row["atomic_heads"]) == 8 for row in report["rounds"])
    assert all(len(head["stages"]) == 11 for row in report["rounds"] for head in row["heads"])
    assert report["rounds"][0]["heads"][0]["decision_source_age_max_seconds"] == 2
    assert report["rounds"][0]["scheduler_elapsed_ms"] == 1200
    assert all("/api/status?profile=v2" in url or url.endswith("/api/freezes/current") for url, _, _ in fixture.requests)
    atomic_times = [elapsed for url, elapsed, _ in fixture.requests if url.endswith("/api/freezes/current")]
    assert atomic_times == [15, 30, 45, 60, 75]
    assert "123.45" not in str(report) and "600001" not in str(report)
    assert fixture.stream_request.headers[-1] == ("Last-Event-ID", "10")
    assert fixture.stream_request.max_response_bytes == 64 * 1024 * 1024


def test_retained_ready_snapshot_and_replayed_initial_events_never_count():
    event, atomic = _batch(7, elapsed=0)
    fixture = _Fixture([(15, event, atomic, 10, "batch")])
    report = fixture.run()
    assert report["status"] == "failed"
    assert report["summary"]["initial_replay_events"] == 1
    assert report["summary"]["complete_published_rounds"] == 0
    assert report["summary"]["stop_reason"] == "observation_deadline"
    assert report["rounds"] == []


def test_duplicate_round_id_under_a_new_sse_id_is_not_another_success():
    event, atomic = _batch(1)
    fixture = _Fixture([(15, event, atomic, 11, "batch"), (30, event, atomic, 12, "batch")])
    report = fixture.run(required=2)
    assert report["status"] == "failed"
    assert report["summary"]["complete_published_rounds"] == report["summary"]["source_age_qualified_rounds"] == 1
    assert report["summary"]["duplicate_rounds"] == 1


@pytest.mark.parametrize("defect", ("profile", "strategy", "preview", "frozen", "five_seats", "generation", "source_time"))
def test_incomplete_or_ineligible_event_is_not_a_complete_published_round(defect):
    def mutate(event, _atomic):
        head = event["profiles"]["v1"]["tomorrow"]
        if defect == "profile":
            event["profiles"].pop("v5")
        elif defect == "strategy":
            event["profiles"]["v1"].pop("d25")
        elif defect == "five_seats":
            head["upserts"].pop()
        elif defect == "generation":
            head["input_versions"]["session_generation"] = "2"
        elif defect == "source_time":
            head["upserts"][0]["source_time"] = "unknown"
        else:
            head["run_mode" if defect == "preview" else "frozen"] = "preview" if defect == "preview" else True
    event, atomic = _batch(1, mutation=mutate)
    report = _Fixture([(15, event, atomic, 11, "batch")]).run()
    assert report["status"] == "failed"
    assert report["summary"]["complete_published_rounds"] == 0
    assert report["reasons"] == [{"code": "incomplete_or_invalid_batch", "count": 1}]


def test_atomic_advancement_is_superseded_and_the_next_new_event_can_still_pass():
    first, _ = _batch(1, elapsed=15)
    second, current = _batch(2, elapsed=30)
    fixture = _Fixture([(15, first, current, 11, "batch"), (30, second, current, 12, "batch")])
    report = fixture.run()
    assert report["status"] == "passed"
    assert report["rounds"][0]["outcome"] == "superseded"
    assert len(report["rounds"][0]["atomic_heads"]) == 8
    assert report["rounds"][1]["outcome"] == "matched"
    assert report["summary"]["complete_published_rounds"] == 2
    assert report["summary"]["identity_matched_rounds"] == 1


def test_same_time_hybrid_advancement_is_superseded():
    event, atomic = _batch(1)
    head = atomic["profiles"]["v1"]["tomorrow"]
    head["stage"] = "hybrid"
    head["freeze_version"] = "hybrid:later"
    report = _Fixture([(15, event, atomic, 11, "batch")]).run()
    assert report["rounds"][0]["outcome"] == "superseded"
    assert report["summary"]["identity_matched_rounds"] == 0


@pytest.mark.parametrize("defect", ("publication", "projection", "inputs", "item_source", "audit"))
def test_atomic_identity_and_committed_audit_failures_are_not_accepted(defect):
    event, atomic = _batch(1)
    head = atomic["profiles"]["v2"]["d25"]
    if defect == "publication":
        head["freeze_version"] = "mismatched"
    elif defect == "projection":
        head["projection_version"] = "mismatched"
    elif defect == "inputs":
        head["input_versions"]["native"] = "mismatched"
    elif defect == "item_source":
        head["items"][0]["quote"]["source_time"] = _START.isoformat()
    else:
        head["pipeline"]["stage_snapshots"].pop()
    report = _Fixture([(15, event, atomic, 11, "batch")]).run()
    assert report["status"] == "failed" and report["summary"]["source_age_qualified_rounds"] == 0
    assert report["summary"]["identity_matched_rounds"] == 0


def test_age_at_decision_and_at_api_delivery_are_separate():
    event, atomic = _batch(1, elapsed=15)
    report = _Fixture([(60, event, atomic, 11, "batch")]).run()
    assert report["summary"]["identity_matched_rounds"] == 1
    assert report["summary"]["decision_source_age_qualified_rounds"] == 1
    assert report["summary"]["delivery_source_age_qualified_rounds"] == 0
    assert report["status"] == "failed"
    head = report["rounds"][0]["heads"][0]
    assert head["decision_source_age_max_seconds"] == 2
    assert head["api_delivery_source_age_max_seconds"] == 47


def test_http_timeout_remains_a_bounded_failed_observation():
    event, atomic = _batch(1)
    fixture = _Fixture([(15, event, atomic, 11, "batch")], fetch_error="request_timeout")
    report = fixture.run()
    assert report["status"] == "failed" and report["summary"]["stop_reason"] == "observation_deadline"
    assert report["reasons"] == [{"code": "request_timeout", "count": 1}]


@pytest.mark.parametrize("event_type,expected", (("resync_required", "event_stream_resync_required"), ("unknown", "event_type_unsupported")))
def test_resync_and_unknown_protocol_stop_without_claiming_new_rounds(event_type, expected):
    event, atomic = _batch(1)
    report = _Fixture([(15, event, atomic, 10, event_type)]).run()
    assert report["summary"]["stop_reason"] == expected
    assert report["summary"]["complete_published_rounds"] == 0


@pytest.mark.parametrize("change", ("runtime", "session"))
def test_runtime_or_session_change_does_not_mix_acceptance_across_identities(change):
    first, first_atomic = _batch(1, elapsed=15)
    second, second_atomic = _batch(2, elapsed=30, generation=2 if change == "session" else 1)
    fixture = _Fixture(
        [(15, first, first_atomic, 11, "batch"), (30, second, second_atomic, 12, "batch")],
        runtime_changed=change == "runtime",
    )
    report = fixture.run(required=2)
    assert report["status"] == "failed"
    assert report["summary"]["stop_reason"] == ("runtime_identity_changed" if change == "runtime" else "session_generation_changed")


def test_fast_batches_only_poll_once_per_fifteen_seconds_and_keep_the_latest_candidate():
    first, first_atomic = _batch(1, elapsed=3)
    second, second_atomic = _batch(2, elapsed=5)
    fixture = _Fixture([(3, first, first_atomic, 11, "batch"), (15, second, second_atomic, 12, "batch")])
    report = fixture.run()
    assert report["status"] == "passed"
    assert report["rounds"][0]["reason"] == "newer_batch_before_poll"
    assert len([url for url, _, _ in fixture.requests if url.endswith("/api/freezes/current")]) == 1


def test_five_total_successes_with_an_unqualified_round_do_not_meet_five_consecutive_target():
    rows = []
    for sequence in range(1, 7):
        event, atomic = _batch(sequence, elapsed=sequence * 15, source_age=31 if sequence == 2 else 2)
        rows.append((sequence * 15, event, atomic, sequence + 10, "batch"))
    report = _Fixture(rows).run(required=5)
    assert report["status"] == "failed"
    assert report["summary"]["source_age_qualified_rounds"] == 5
    assert report["summary"]["current_consecutive_qualified_rounds"] == 4
    assert report["summary"]["maximum_consecutive_qualified_rounds"] == 4


@pytest.mark.parametrize("interruption", ("invalid", "mismatch", "superseded", "audit"))
def test_unverified_round_interrupts_a_previously_qualified_streak(interruption):
    first, first_api = _batch(1, elapsed=15)
    middle, middle_api = _batch(2, elapsed=30)
    last, last_api = _batch(3, elapsed=45)
    if interruption == "invalid":
        middle["profiles"].pop("v5")
    elif interruption == "mismatch":
        middle_api["profiles"]["v2"]["tomorrow"]["projection_version"] = "different"
    elif interruption == "superseded":
        middle_api = last_api
    else:
        middle_api["profiles"]["v2"]["tomorrow"]["pipeline"]["stage_snapshots"].pop()
    report = _Fixture([
        (15, first, first_api, 11, "batch"), (30, middle, middle_api, 12, "batch"),
        (45, last, last_api, 13, "batch"),
    ]).run(required=2)
    assert report["status"] == "failed"
    assert report["summary"]["source_age_qualified_rounds"] == 2
    assert report["summary"]["current_consecutive_qualified_rounds"] == 1
    assert report["summary"]["maximum_consecutive_qualified_rounds"] == 1


def test_sequence_gap_restarts_qualification_instead_of_using_accumulated_count():
    first, first_api = _batch(1, elapsed=15)
    third, third_api = _batch(3, elapsed=30)
    report = _Fixture([(15, first, first_api, 11, "batch"), (30, third, third_api, 12, "batch")]).run(required=2)
    assert report["status"] == "failed" and report["summary"]["source_age_qualified_rounds"] == 2
    assert report["summary"]["current_consecutive_qualified_rounds"] == 1
    assert report["reasons"] == [{"code": "published_round_sequence_gap", "count": 1}]


def test_duplicate_round_does_not_interrupt_consecutive_distinct_publications():
    first, first_api = _batch(1, elapsed=15)
    second, second_api = _batch(2, elapsed=45)
    report = _Fixture([
        (15, first, first_api, 11, "batch"), (30, first, first_api, 12, "batch"),
        (45, second, second_api, 13, "batch"),
    ]).run(required=2)
    assert report["status"] == "passed"
    assert report["summary"]["current_consecutive_qualified_rounds"] == 2
    assert report["summary"]["source_age_qualified_rounds"] == 2
    assert report["summary"]["duplicate_rounds"] == 1


def test_published_sequence_regression_stops_instead_of_counting_a_new_distinct_round():
    first, first_api = _batch(2, elapsed=15)
    old, old_api = _batch(1, elapsed=30)
    report = _Fixture([(15, first, first_api, 11, "batch"), (30, old, old_api, 12, "batch")]).run(required=2)
    assert report["status"] == "failed"
    assert report["summary"]["stop_reason"] == "published_round_sequence_regressed"
    assert report["summary"]["current_consecutive_qualified_rounds"] == 0


def test_invalid_batch_cannot_leave_an_older_unverified_pending_round_to_complete_the_target():
    first, first_api = _batch(1, elapsed=15)
    pending, pending_api = _batch(2, elapsed=20)
    invalid, _ = _batch(3, elapsed=30)
    invalid["profiles"].pop("v5")
    report = _Fixture([
        (15, first, first_api, 11, "batch"), (20, pending, pending_api, 12, "batch"),
        (30, invalid, pending_api, 13, "batch"),
    ]).run(required=2)
    assert report["status"] == "failed"
    assert report["summary"]["source_age_qualified_rounds"] == 1
    assert report["summary"]["current_consecutive_qualified_rounds"] == 0
    assert report["rounds"][-1]["reason"] == "invalid_batch_before_poll"


@pytest.mark.parametrize("stream_mode", ("heartbeat", "silent"))
def test_real_http_stream_deadline_returns_partial_round_evidence_through_native_worker(stream_mode, record_property):
    event, _atomic = _batch(1)
    stop = threading.Event()
    request_times = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            request_times.append((self.path, time.monotonic()))
            if self.path.startswith("/api/status"):
                body = json.dumps({
                    "runtime_version": "fixture-runtime", "available_profiles": list(_PROFILES),
                    "phase": "before_close", "events": {"sequence": 10},
                    "scheduler": {"recommendation_round": {"sequence": 0, "outcome": "running"}},
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                for body in (_frame(event),):
                    self.wfile.write(f"{len(body):X}\r\n".encode() + body + b"\r\n")
                    self.wfile.flush()
                while not stop.wait(0.1):
                    if stream_mode == "heartbeat":
                        body = b": heartbeat\n\n"
                        self.wfile.write(f"{len(body):X}\r\n".encode() + body + b"\r\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        started = time.monotonic()
        try:
            result = RunBoundedCommand(WorkerArguments((
                sys.executable, "-m", "scripts.recommendation.web.web_health", "--base-url",
                f"http://127.0.0.1:{server.server_port}", "--complete-rounds", "1",
                "--observation-seconds", "3", "--interval-seconds", "15", "--timeout-seconds", "1",
            )), timeout=25, environment=dict(os.environ))
        finally:
            elapsed = time.monotonic() - started
            record_property("process_elapsed_seconds", elapsed)
            record_property("http_requests", [(path, at - started) for path, at in request_times])
        assert result.stdout, result.stderr
        report = json.loads(result.stdout)
        assert result.returncode == 1 and report["status"] == "failed"
        assert report["summary"]["stop_reason"] == "observation_deadline"
        assert report["summary"]["complete_published_rounds"] == 1
        assert len(report["rounds"]) == 1
        assert any(path.startswith("/api/events") for path, _at in request_times)
        first_request_after = request_times[0][1] - started
        record_property("first_http_after_start_seconds", first_request_after)
        record_property("http_observation_elapsed_seconds", elapsed - first_request_after)
        assert elapsed - first_request_after < 5
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


@pytest.mark.parametrize("outcome", ("accepted", "gzip_large", "age_unqualified", "atomic_advanced", "atomic_timeout"))
def test_real_requests_batch_poll_and_exit_keep_bound_evidence(outcome, record_property):
    # Import startup is measured separately above; here the Requests read, poll,
    # decode and response close paths all use actual loopback HTTP connections.
    from trader.bootstrap import OpenExternalHttp
    from scripts.recommendation.web.web_health import FetchError, _fetch_mapping

    at = datetime.now(ZoneInfo("Asia/Shanghai"))
    event, atomic = _batch(1, elapsed=(at - _START).total_seconds(), source_age=31 if outcome == "age_unqualified" else 2)
    if outcome == "gzip_large":
        event["diagnostic_padding"] = "完整帧" * 50_000
    if outcome == "atomic_advanced":
        atomic = _batch(2, elapsed=(at - _START).total_seconds() + 1)[1]
    offset = [0.0]
    stopped = threading.Event()
    requests = []

    def monotonic():
        return time.monotonic() + offset[0]

    class HeaderAdvanceTransport(RequestsHttpTransport):
        def execute(self, request):
            receive_headers = request.receive_headers
            if receive_headers is not None:
                def advance_headers(status, headers):
                    # Reach the production 15s throttle using a logical clock;
                    # the actual driver still enforces the remaining 3s socket
                    # budget and performs all body reads and nested poll I/O.
                    offset[0] = 15.0
                    receive_headers(status, headers)
                request = replace(request, receive_headers=advance_headers)
            return super().execute(request)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            requests.append((self.path, time.monotonic()))
            if self.path.startswith("/api/events"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                if outcome == "gzip_large":
                    self.send_header("Content-Encoding", "gzip")
                self.end_headers()
                compressor = zlib.compressobj(wbits=31) if outcome == "gzip_large" else None

                def write(body):
                    if compressor is not None:
                        body = compressor.compress(body) + compressor.flush(zlib.Z_SYNC_FLUSH)
                    self.wfile.write(f"{len(body):X}\r\n".encode() + body + b"\r\n")
                    self.wfile.flush()

                try:
                    write(_frame(event))
                    while not stopped.wait(0.05):
                        write(b": heartbeat\n\n")
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            if self.path.startswith("/api/freezes/current"):
                if outcome == "atomic_timeout":
                    stopped.wait(2)
                payload = atomic
            else:
                payload = {
                    "runtime_version": "fixture-runtime", "available_profiles": list(_PROFILES),
                    "phase": "before_close", "events": {"sequence": 11 if offset[0] else 10},
                    "scheduler": {"recommendation_round": {"sequence": 1, "outcome": "published", "elapsed_ms": 1250}},
                }
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args):
            pass

    def fetch(url, timeout):
        try:
            return _fetch_mapping(url, timeout)
        except FetchError as exc:
            raise ObservationFetchError(exc.error_code) from exc

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        started = time.monotonic()
        with HeaderAdvanceTransport(monotonic=monotonic) as transport:
            report = ObserveCompleteRounds(
                f"http://127.0.0.1:{server.server_port}", required_rounds=1, observation_seconds=18,
                interval_seconds=15, timeout_seconds=0.5, scoring_profile="v2", fetch_mapping=fetch,
                transport=transport, monotonic=monotonic,
            )
        elapsed = time.monotonic() - started
        record_property("physical_elapsed_seconds", elapsed)
        record_property("logical_header_advance_seconds", 15)
        record_property("requests", [path for path, _at in requests])
        record_property("request_elapsed_seconds", [at - started for _path, at in requests])
        assert elapsed < 5
        assert report["summary"]["complete_published_rounds"] == 1
        assert any(path.startswith("/api/freezes/current") for path, _at in requests)
        if outcome in {"accepted", "gzip_large"}:
            assert report["status"] == "passed"
            assert report["summary"]["stop_reason"] == "target_reached"
            assert len(report["rounds"][0]["heads"]) == 8
            assert all(len(head["stages"]) == 11 for head in report["rounds"][0]["heads"])
        else:
            assert report["status"] == "failed"
            assert report["summary"]["stop_reason"] == "observation_deadline"
            assert len(report["rounds"]) == 1
            if outcome == "age_unqualified":
                assert len(report["rounds"][0]["heads"]) == 8
            elif outcome == "atomic_advanced":
                assert report["rounds"][0]["outcome"] == "superseded"
            else:
                assert {reason["code"] for reason in report["reasons"]} == {"request_timeout"}
        # Independent short-lived fetch transports remain separate from the
        # SSE transport, and their explicit context owner also closes normally.
        with OpenExternalHttp() as independent:
            assert independent is not transport
    finally:
        stopped.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)
