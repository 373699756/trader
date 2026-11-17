from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from threading import Event, Thread

import pytest

from scripts.download.company import company_pipeline_audit
from scripts.download.company import company_resume
from trader.download.domain.company_evidence import (
    CompanyDocumentFailure,
    CompanySourceFailure,
)
from trader.download.infra.company_raw_files import CompanyRawFiles
from trader.download.domain.company_sync import CompanySourceCancelled
from trader.infra.http_contracts import HttpTimeout


def test_default_company_measurement_rejects_unadmitted_source_before_io(tmp_path, monkeypatch):
    monkeypatch.setattr(company_pipeline_audit, "OpenExternalHttp", lambda: pytest.fail("default source client opened"))
    configuration, report = tmp_path / "missing-runtime.json", tmp_path / "report.json"
    with pytest.raises(ValueError, match="company_structured_source_admission_pending"):
        company_pipeline_audit.MeasureCompanySync(configuration, report)
    assert not tuple(tmp_path.iterdir())








def test_candidate_reservation_scan_is_observed_without_counting_it_as_legacy_usage(tmp_path):
    raw = CompanyRawFiles(
        tmp_path / "private-material",
        capacity=10_000,
        analysis_reserve=1000,
        free_reserve=1,
        maximum_document_bytes=64 * 1024**2,
        scan_reservations_once=True,
    )
    measurement = company_pipeline_audit._CompanySyncMeasurement()
    original_scan = CompanyRawFiles._reservation_usage
    with ExitStack() as observers:
        company_pipeline_audit._observe_company_operations(observers, measurement)
        with raw.reserve(raw.root / "documents" / "private-evidence", 100):
            pass
    operations = {row["operation"]: row for row in measurement.report()}
    assert operations["raw_reservation_scan"]["calls"] == 1
    assert operations["raw_reservation_scan"]["failures"] == []
    assert operations["raw_usage"]["calls"] == operations["raw_size_scan"]["calls"] == 0
    assert CompanyRawFiles._reservation_usage is original_scan and not raw._reservations
    assert "private" not in json.dumps(measurement.report())




def test_operation_failure_categories_remain_bounded_when_the_known_taxonomy_grows(monkeypatch):
    reasons = tuple(f"disclosure_document_case_{index}" for index in range(80))
    monkeypatch.setattr(company_pipeline_audit, "_DOCUMENT_FAILURES", frozenset(reasons))
    measurement = company_pipeline_audit._CompanySyncMeasurement()
    for reason in reasons:
        with pytest.raises(CompanyDocumentFailure), measurement.operation("extract_standard"):
            raise CompanyDocumentFailure(reason)
    operation = next(row for row in measurement.report() if row["operation"] == "extract_standard")
    assert operation["calls"] == 80 and len(operation["failures"]) <= 32
    assert sum(row["calls"] for row in operation["failures"]) == 80


@pytest.mark.parametrize("error, category", (
    (CompanyDocumentFailure("disclosure_document_ocr_required"), "disclosure_document_ocr_required"),
    (CompanyDocumentFailure("disclosure_document_private_payload"), "document_other"),
    (HttpTimeout("private-transport-payload"), "timeout"),
    (InterruptedError("private-cancellation-payload"), "interrupted"),
    (CompanySourceCancelled("private-cancellation-payload"), "cancelled"),
    (CompanySourceFailure("timeout"), "timeout"),
    (CompanySourceFailure("schema_invalid"), "schema_invalid"),
    (CompanySourceFailure("private-unknown-category"), "source_other"),
))
def test_measurement_keeps_known_failure_categories_and_hides_unknown_payload(error, category):
    assert company_pipeline_audit._failure_category(error) == category


def test_running_heartbeat_observes_an_unfinished_operation_without_waiting_for_completion():
    clock = [0.0]
    measurement = company_pipeline_audit._CompanySyncMeasurement(monotonic=lambda: clock[0], snapshot_interval_seconds=0.01)
    snapshots = []
    entered, release, observed = Event(), Event(), Event()

    def snapshot():
        assert measurement._lock.acquire(blocking=False)
        measurement._lock.release()
        snapshots.append(next(row for row in measurement.report() if row["operation"] == "raw_usage"))
        observed.set()

    def business():
        entered.set()
        assert release.wait(timeout=5)
        return 7

    with ThreadPoolExecutor(max_workers=1) as workers:
        pending = workers.submit(measurement.wrap("raw_usage", business))
        try:
            assert entered.wait(timeout=5)
            clock[0] = 45.0
            measurement.set_snapshot_writer(snapshot)
            assert observed.wait(timeout=5)
            assert not pending.done()
            assert snapshots[0]["calls"] == 0 and snapshots[0]["inflight_calls"] == 1
            assert snapshots[0]["oldest_inflight_seconds"] == 45.0
            assert snapshots[0]["inflight_duration_complete"]
        finally:
            release.set()
            assert pending.result(timeout=5) == 7
            measurement.close_snapshots()
    assert measurement._snapshot_thread is not None and not measurement._snapshot_thread.is_alive()
    final = next(row for row in measurement.report() if row["operation"] == "raw_usage")
    assert final["calls"] == 1 and final["inflight_calls"] == 0
    assert final["oldest_inflight_seconds"] == 0


@pytest.mark.parametrize("business_fails", (False, True))
@pytest.mark.parametrize("report_error", (OSError("private-report-path"), ValueError("private-json-payload")))
def test_running_snapshot_failure_is_disabled_once_without_changing_the_business_outcome(capsys, business_fails, report_error):
    measurement = company_pipeline_audit._CompanySyncMeasurement(snapshot_interval_seconds=0.01)
    attempts = []
    original = RuntimeError("private-business-error")
    attempted = Event()

    def failed_snapshot():
        attempts.append(True)
        attempted.set()
        raise report_error

    def business():
        if business_fails:
            raise original
        return 42

    measurement.set_snapshot_writer(failed_snapshot)
    assert attempted.wait(timeout=5)
    measurement.close_snapshots()
    if business_fails:
        with pytest.raises(RuntimeError) as raised:
            measurement.wrap("raw_usage", business)()
        assert raised.value is original
    else:
        assert measurement.wrap("raw_usage", business)() == 42
    assert measurement.wrap("raw_usage", lambda: 1)() == 1
    assert measurement.snapshot_failed and attempts == [True]
    measurement.close_snapshots()
    errors = capsys.readouterr().err
    assert errors.count("running_report_write_failed") == 1 and "private" not in errors


def test_closing_snapshots_waits_for_the_inflight_writer_before_the_final_report():
    measurement = company_pipeline_audit._CompanySyncMeasurement(snapshot_interval_seconds=0.01)
    started, release, close_started = Event(), Event(), Event()
    publications = []

    def snapshot():
        started.set()
        assert release.wait(timeout=5)
        publications.append("running")

    def final():
        close_started.set()
        measurement.close_snapshots()
        publications.append("final")

    measurement.set_snapshot_writer(snapshot)
    with ThreadPoolExecutor(max_workers=1) as workers:
        assert started.wait(timeout=5)
        finished = workers.submit(final)
        assert close_started.wait(timeout=5)
        assert not finished.done()
        release.set()
        finished.result(timeout=5)
    assert publications == ["running", "final"]
    assert measurement._snapshot_thread is not None and not measurement._snapshot_thread.is_alive()






def test_inflight_duration_tracking_is_bounded_without_losing_call_counts(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(company_pipeline_audit, "_MAX_PENDING_STARTS_PER_OPERATION", 2)
    measurement = company_pipeline_audit._CompanySyncMeasurement(monotonic=lambda: clock[0])
    with ExitStack() as operations:
        for instant in (0.0, 1.0, 2.0):
            clock[0] = instant
            operations.enter_context(measurement.operation("facts_compile"))
        clock[0] = 12.0
        row = next(row for row in measurement.report() if row["operation"] == "facts_compile")
        assert row["inflight_calls"] == 3 and row["untracked_inflight_calls"] == 1
        assert not row["inflight_duration_complete"] and row["oldest_inflight_seconds"] is None
    row = next(row for row in measurement.report() if row["operation"] == "facts_compile")
    assert row["calls"] == 3 and row["inflight_calls"] == 0 and row["inflight_duration_complete"]


def test_thread_start_failure_is_reported_without_starting_or_changing_business(capsys, monkeypatch):
    class UnstartedThread(Thread):
        def start(self):
            raise RuntimeError("private-thread-start-error")

    monkeypatch.setattr(company_pipeline_audit, "Thread", UnstartedThread)
    measurement = company_pipeline_audit._CompanySyncMeasurement()
    measurement.set_snapshot_writer(lambda: pytest.fail("failed start must not publish"))
    assert measurement.wrap("raw_usage", lambda: 42)() == 42
    measurement.close_snapshots()
    assert measurement.snapshot_failed
    assert "running_report_thread_start_failed" in capsys.readouterr().err










@pytest.mark.parametrize("arguments", (
    ["--live-sync"],
    ["--runtime-config", "config/runtime.json"],
    ["--report", "/outside/report.json"],
    ["--fill"],
    ["--database", "missing.sqlite3"],
    ["--budget-seconds", "30"],
))
def test_company_resume_requires_isolated_inputs_and_rejects_retired_live_options(monkeypatch, arguments):
    monkeypatch.setattr(sys, "argv", ["company-resume", *arguments])
    with pytest.raises(SystemExit) as raised:
        company_resume.Main()
    assert raised.value.code == 2
