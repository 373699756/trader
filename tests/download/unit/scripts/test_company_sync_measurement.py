from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from threading import Event
from zoneinfo import ZoneInfo

import pytest

from scripts.download.company import company_pipeline_audit
from scripts.download.company import company_resume
from trader.download.domain.company_evidence import (
    CompanyDocumentExtraction,
    CompanyDocumentFailure,
    CompanySourceFailure,
    Disclosure,
    DisclosurePage,
    DisclosurePartition,
    StructuredCompanyPage,
    StructuredCompanyQuery,
)
from trader.download.domain.company_annual_income import CompanyAnnualIncomeRequest
from trader.download.infra.company_disclosure_source import CninfoMarketDisclosureSource
from trader.download.infra.company_evidence_index import SQLiteCompanyEvidenceIndex
from trader.download.infra import company_evidence_index
from trader.download.infra.company_facts_index import SQLiteCompanyFactsIndex
from trader.download.infra.company_http import CompanyRequestPacer
from trader.download.infra.company_raw_files import CompanyLocalDocument, CompanyRawFiles
from trader.download.infra.company_structured_source import EastmoneyCompanySource
from trader.entrypoints import cli
from trader.infra.http_contracts import HttpResponse, HttpTimeout
from trader.infra.sqlite_connection import SqliteConnection, SqliteSession, SqliteTransaction

_NOW = datetime(2026, 11, 5, 16, tzinfo=ZoneInfo("Asia/Shanghai"))


def _paths(tmp_path):
    configuration = tmp_path / "product/config/runtime.json"
    configuration.parent.mkdir(parents=True)
    configuration.write_text("{}", encoding="utf-8")
    return configuration, tmp_path / "live-report.json"


def _disclosure():
    return Disclosure(
        "cninfo", "private-evidence", ("600001",), "本公司公告", _NOW,
        "https://private.invalid/must-not-report", "private-digest",
    )


def test_live_measurement_uses_real_cli_entrypoint_and_measures_nested_routes(tmp_path, monkeypatch, capsys):
    configuration, report = _paths(tmp_path)
    source = object.__new__(CninfoMarketDisclosureSource)
    structured = object.__new__(EastmoneyCompanySource)
    raw = object.__new__(CompanyRawFiles)
    pacer = object.__new__(CompanyRequestPacer)
    collection = object.__new__(SQLiteCompanyEvidenceIndex)
    facts = object.__new__(SQLiteCompanyFactsIndex)
    local = CompanyLocalDocument(tmp_path, tmp_path / "private.pdf", b"a" * 32, 10)
    text = "本公司已核实的结构化公告文字。" * 10
    extraction = CompanyDocumentExtraction(text, "text", b"a" * 32, "fixture")
    arguments = []

    def fetch(instance, partition, *, order=""):
        instance._fetch_page(partition, order=order)
        return DisclosurePage((), 0, False)

    def structured_fetch(instance, query, *, observed_at):
        return StructuredCompanyPage(query.component, 1, 1, 0)

    def download(instance, item):
        instance._transfer_document(item, local.directory)

    def extract(instance, item, *, route="standard"):
        return instance._analyze_document(local, route)

    def analyze(instance, current, route):
        return instance._ocr(current.path, current.payload_hash) if route == "ocr" else extraction

    def usage(instance):
        return sum(instance._size(tmp_path / name) for name in ("documents", "text", "failed"))

    monkeypatch.setattr(CninfoMarketDisclosureSource, "fetch", fetch)
    monkeypatch.setattr(CninfoMarketDisclosureSource, "_fetch_page", lambda *args, **kwargs: HttpResponse(200, b""))
    monkeypatch.setattr(EastmoneyCompanySource, "fetch", structured_fetch)
    monkeypatch.setattr(EastmoneyCompanySource, "pledge_day", lambda *args: _NOW.date())
    monkeypatch.setattr(EastmoneyCompanySource, "annual_income", lambda *args, **kwargs: ())
    monkeypatch.setattr(EastmoneyCompanySource, "news", lambda *args, **kwargs: ())
    monkeypatch.setattr(CninfoMarketDisclosureSource, "download_document", download)
    monkeypatch.setattr(CninfoMarketDisclosureSource, "_transfer_document", lambda *args: HttpResponse(200, b""))
    monkeypatch.setattr(CninfoMarketDisclosureSource, "extract_document", extract)
    monkeypatch.setattr(CninfoMarketDisclosureSource, "_analyze_document", analyze)
    monkeypatch.setattr(CninfoMarketDisclosureSource, "_ocr", lambda *args: extraction)
    monkeypatch.setattr(CompanyRawFiles, "usage", usage)
    monkeypatch.setattr(CompanyRawFiles, "_size", staticmethod(lambda directory: 3))
    monkeypatch.setattr(CompanyRawFiles, "digest", staticmethod(lambda path: b"d" * 32))
    monkeypatch.setattr(CompanyRequestPacer, "wait", lambda *args: None)
    monkeypatch.setattr(SQLiteCompanyEvidenceIndex, "publish", lambda *args, **kwargs: 1)
    monkeypatch.setattr(SQLiteCompanyFactsIndex, "publish", lambda *args, **kwargs: None)
    monkeypatch.setattr(SQLiteCompanyEvidenceIndex, "initialize", lambda *args: None)
    monkeypatch.setattr(SQLiteCompanyEvidenceIndex, "prepare_publication", lambda *args: None)
    monkeypatch.setattr(company_evidence_index, "ReadCompanySnapshotRows", lambda *args: ())
    monkeypatch.setattr(company_evidence_index, "CompileCompanyFacts", lambda *args: None)
    monkeypatch.setattr(SQLiteCompanyFactsIndex, "publish_file", lambda *args: None)
    monkeypatch.setattr(SQLiteCompanyEvidenceIndex, "_release_files", lambda *args: None)
    monkeypatch.setattr(CompanyRawFiles, "_reservation_usage", lambda *args: (0, 0, 0))
    original_digest, original_run = CompanyRawFiles.digest, SqliteConnection.run

    def main(argv):
        arguments.append(argv)
        source.fetch(DisclosurePartition("sse", _NOW.date(), _NOW.date()))
        for component in ("financial", "pledge", "unlock"):
            structured.fetch(StructuredCompanyQuery(component, _NOW.date(), _NOW.date()), observed_at=_NOW)
        structured.pledge_day(_NOW)
        structured.annual_income(CompanyAnnualIncomeRequest("600001", (_NOW.date().replace(year=2025, month=12, day=31),)), observed_at=_NOW)
        structured.news("600001", observed_at=_NOW)
        source.download_document(_disclosure())
        for route in ("standard", "ocr", "limits"):
            source.extract_document(_disclosure(), route=route)
        assert raw.usage() == 9
        assert raw.digest(local.path) == b"d" * 32
        pacer.wait()
        collection.initialize()
        collection.prepare_publication(("600001",), _NOW)
        company_evidence_index.ReadCompanySnapshotRows(None, ("600001",), _NOW, b"a" * 32)
        company_evidence_index.CompileCompanyFacts(None, None)
        collection.publish(("600001",), _NOW)
        facts.publish((), {}, None, _NOW)
        facts.publish_file()
        collection._release_files()
        assert raw._reservation_usage(tmp_path) == (0, 0, 0)
        with SqliteSession(tmp_path / "fixture.sqlite3") as connection, SqliteTransaction(connection):
            connection.run("CREATE TABLE fixture(value INTEGER)")
            connection.run("INSERT INTO fixture VALUES(1)")
        return 0

    monkeypatch.setattr(cli, "Main", main)
    assert company_pipeline_audit.MeasureCompanySync(configuration, report, fill_missing=True) == 0
    assert arguments == [["--config", str(configuration), "sync", "company", "--fill"]]
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["writes_current_company_data"] and not payload["isolated_sample"]
    assert payload["cli_exit_code"] == 0 and not payload["field_correctness_verified"]
    operations = {row["operation"]: row for row in payload["operations"]}
    expected = {name: 1 for name in operations}
    expected["raw_size_scan"] = 3
    assert {name: row["calls"] for name, row in operations.items()} == expected
    assert all(row["wall_seconds"] >= 0 and row["thread_cpu_seconds"] >= 0 and not row["failures"] for row in operations.values())
    assert "inclusive" in payload["operation_timing"] and not payload["thread_cpu_includes_child_processes"]
    assert "no-file" in payload["operation_boundaries"]["facts_seal"]
    assert not payload["database"]["wal_sampling_enabled"] and payload["database"]["connections"] == 1
    assert payload["database"]["statements"] >= 2
    assert CompanyRawFiles.digest is original_digest and raw.digest(local.path) == b"d" * 32
    assert CninfoMarketDisclosureSource.fetch is fetch and SqliteConnection.run is original_run
    assert "private" not in report.read_text(encoding="utf-8")
    assert "写入当前 company 数据" in capsys.readouterr().err


def test_publication_phase_failure_restores_all_new_owners_and_keeps_only_failure_category(tmp_path, monkeypatch):
    configuration, report = _paths(tmp_path)
    original = RuntimeError("private-phase-payload-https://secret.invalid")

    def failed_compile(*args, **kwargs):
        raise original

    def main(argv):
        company_evidence_index.CompileCompanyFacts("private-input", "private-policy")

    monkeypatch.setattr(company_evidence_index, "CompileCompanyFacts", failed_compile)
    monkeypatch.setattr(cli, "Main", main)
    owners = (
        (SQLiteCompanyEvidenceIndex, "initialize"),
        (SQLiteCompanyEvidenceIndex, "prepare_publication"),
        (company_evidence_index, "ReadCompanySnapshotRows"),
        (company_evidence_index, "CompileCompanyFacts"),
        (SQLiteCompanyFactsIndex, "publish_file"),
        (SQLiteCompanyEvidenceIndex, "_release_files"),
        (CompanyRawFiles, "_reservation_usage"),
    )
    originals = tuple(getattr(owner, name) for owner, name in owners)
    with pytest.raises(RuntimeError) as raised:
        company_pipeline_audit.MeasureCompanySync(configuration, report)
    assert raised.value is original
    assert tuple(getattr(owner, name) for owner, name in owners) == originals
    payload = json.loads(report.read_text(encoding="utf-8"))
    operation = next(row for row in payload["operations"] if row["operation"] == "facts_compile")
    assert operation["calls"] == 1 and operation["failures"] == [{"category": "runtime_error", "calls": 1}]
    assert "private" not in report.read_text(encoding="utf-8")


def test_candidate_reservation_scan_is_observed_without_counting_it_as_legacy_usage(tmp_path):
    raw = CompanyRawFiles(
        tmp_path / "private-material",
        capacity=10_000,
        analysis_reserve=1000,
        free_reserve=1,
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


def test_live_measurement_aggregates_concurrent_failures_without_retaining_parameters(tmp_path, monkeypatch):
    configuration, report = _paths(tmp_path)
    source = object.__new__(CninfoMarketDisclosureSource)

    def fetch(instance, partition, *, order=""):
        if partition.page % 3 == 0:
            raise CompanySourceFailure("rate_limited")
        return DisclosurePage((), 0, False)

    def main(argv):
        def request(page):
            try:
                source.fetch(DisclosurePartition("sse", _NOW.date(), _NOW.date(), page=page))
                return False
            except CompanySourceFailure:
                return True

        with ThreadPoolExecutor(max_workers=4) as workers:
            assert sum(workers.map(request, range(1, 61))) == 20
        return 1

    monkeypatch.setattr(CninfoMarketDisclosureSource, "fetch", fetch)
    monkeypatch.setattr(cli, "Main", main)
    assert company_pipeline_audit.MeasureCompanySync(configuration, report) == 1
    payload = json.loads(report.read_text(encoding="utf-8"))
    operation = next(row for row in payload["operations"] if row["operation"] == "directory_fetch")
    assert operation["calls"] == 60 and operation["failures"] == [{"category": "rate_limited", "calls": 20}]
    assert payload["cli_exit_code"] == 1 and payload["status"] == "degraded"
    assert CninfoMarketDisclosureSource.fetch is fetch


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
    (HttpTimeout("private-transport-payload"), "timeout"),
    (CompanySourceFailure("timeout"), "timeout"),
    (CompanySourceFailure("schema_invalid"), "schema_invalid"),
    (CompanySourceFailure("private-unknown-category"), "source_other"),
))
def test_measurement_keeps_known_failure_categories_and_hides_unknown_payload(error, category):
    assert company_pipeline_audit._failure_category(error) == category


def test_running_snapshots_use_the_injected_clock_and_write_outside_the_counter_lock():
    clock = [0.0]
    measurement = company_pipeline_audit._CompanySyncMeasurement(monotonic=lambda: clock[0])
    snapshots = []

    def snapshot():
        assert measurement._lock.acquire(blocking=False)
        measurement._lock.release()
        snapshots.append(next(row["calls"] for row in measurement.report() if row["operation"] == "raw_usage"))

    measurement.set_snapshot_writer(snapshot)
    counted = measurement.wrap("raw_usage", lambda: 7)
    for observed in (29.0, 30.0, 59.0, 60.0):
        clock[0] = observed
        assert counted() == 7
    assert snapshots == [2, 4]
    measurement.close_snapshots()
    clock[0] = 100.0
    assert counted() == 7 and snapshots == [2, 4]


@pytest.mark.parametrize("business_fails", (False, True))
def test_running_snapshot_failure_is_disabled_once_without_changing_the_business_outcome(capsys, business_fails):
    clock = [0.0]
    measurement = company_pipeline_audit._CompanySyncMeasurement(monotonic=lambda: clock[0])
    attempts = []
    original = RuntimeError("private-business-error")

    def failed_snapshot():
        attempts.append(True)
        raise OSError("private-report-path")

    def business():
        if business_fails:
            raise original
        return 42

    measurement.set_snapshot_writer(failed_snapshot)
    clock[0] = 30.0
    if business_fails:
        with pytest.raises(RuntimeError) as raised:
            measurement.wrap("raw_usage", business)()
        assert raised.value is original
    else:
        assert measurement.wrap("raw_usage", business)() == 42
    clock[0] = 90.0
    assert measurement.wrap("raw_usage", lambda: 1)() == 1
    assert measurement.snapshot_failed and attempts == [True]
    measurement.close_snapshots()
    errors = capsys.readouterr().err
    assert errors.count("running_report_write_failed") == 1 and "private" not in errors


def test_closing_snapshots_waits_for_the_inflight_writer_before_the_final_report():
    clock = [0.0]
    measurement = company_pipeline_audit._CompanySyncMeasurement(monotonic=lambda: clock[0])
    started, release, close_started = Event(), Event(), Event()
    publications = []

    def snapshot():
        started.set()
        assert release.wait(timeout=2)
        publications.append("running")

    def final():
        close_started.set()
        measurement.close_snapshots()
        publications.append("final")

    measurement.set_snapshot_writer(snapshot)
    clock[0] = 30.0
    with ThreadPoolExecutor(max_workers=2) as workers:
        operation = workers.submit(measurement.wrap("raw_usage", lambda: 1))
        assert started.wait(timeout=2)
        finished = workers.submit(final)
        assert close_started.wait(timeout=2)
        release.set()
        assert operation.result(timeout=2) == 1
        finished.result(timeout=2)
    assert publications == ["running", "final"]


def test_live_measurement_refreshes_running_reports_and_finalizes_the_same_schema(tmp_path, monkeypatch):
    configuration, report = _paths(tmp_path)
    clock = [0.0]
    measurement = company_pipeline_audit._CompanySyncMeasurement(monotonic=lambda: clock[0])
    monkeypatch.setattr(company_pipeline_audit, "_CompanySyncMeasurement", lambda: measurement)
    source = object.__new__(CninfoMarketDisclosureSource)
    monkeypatch.setattr(CninfoMarketDisclosureSource, "fetch", lambda *args, **kwargs: DisclosurePage((), 0, False))
    observed = []

    def main(argv):
        initial = json.loads(report.read_text(encoding="utf-8"))
        assert initial["status"] == "running" and not initial["database_statistics_final"]
        assert initial["database"] == {} and initial["snapshot_interval_seconds"] == 30.0
        for instant in (30.0, 60.0):
            clock[0] = instant
            source.fetch(DisclosurePartition("sse", _NOW.date(), _NOW.date()))
            observed.append(json.loads(report.read_text(encoding="utf-8")))
        return 0

    monkeypatch.setattr(cli, "Main", main)
    assert company_pipeline_audit.MeasureCompanySync(configuration, report) == 0
    final = json.loads(report.read_text(encoding="utf-8"))
    assert final["status"] == "passed" and final["database_statistics_final"]
    assert not final["snapshot_failed"] and final["database"]["completed"]
    for expected, snapshot in enumerate(observed, start=1):
        assert set(snapshot) == set(final) and snapshot["status"] == "running"
        assert not snapshot["database_statistics_final"] and snapshot["database"] == {}
        operation = next(row for row in snapshot["operations"] if row["operation"] == "directory_fetch")
        assert operation["calls"] == expected


def test_failed_running_report_is_marked_in_the_successful_final_report(tmp_path, monkeypatch, capsys):
    configuration, report = _paths(tmp_path)
    clock = [0.0]
    measurement = company_pipeline_audit._CompanySyncMeasurement(monotonic=lambda: clock[0])
    monkeypatch.setattr(company_pipeline_audit, "_CompanySyncMeasurement", lambda: measurement)
    source = object.__new__(CninfoMarketDisclosureSource)
    monkeypatch.setattr(CninfoMarketDisclosureSource, "fetch", lambda *args, **kwargs: DisclosurePage((), 0, False))
    write = company_pipeline_audit.AtomicWriteJson
    statuses = []

    def report_writer(path, payload, *, indent):
        statuses.append(payload["status"])
        if payload["status"] == "running":
            raise OSError("private-snapshot-error")
        write(path, payload, indent=indent)

    def main(argv):
        for instant in (30.0, 60.0):
            clock[0] = instant
            source.fetch(DisclosurePartition("sse", _NOW.date(), _NOW.date()))
        return 0

    monkeypatch.setattr(company_pipeline_audit, "AtomicWriteJson", report_writer)
    monkeypatch.setattr(cli, "Main", main)
    assert company_pipeline_audit.MeasureCompanySync(configuration, report) == 0
    final = json.loads(report.read_text(encoding="utf-8"))
    assert final["status"] == "passed" and final["snapshot_failed"] and final["database_statistics_final"]
    assert statuses == ["running", "passed"]
    errors = capsys.readouterr().err
    assert errors.count("running_report_write_failed") == 1 and "private" not in errors


@pytest.mark.parametrize("error", (RuntimeError("private-payload-https://secret"), KeyboardInterrupt()))
def test_live_measurement_restores_hooks_writes_report_and_reraises_original_error(tmp_path, monkeypatch, error):
    configuration, report = _paths(tmp_path)
    source = object.__new__(CninfoMarketDisclosureSource)

    def fetch(instance, partition, *, order=""):
        raise error

    def main(argv):
        source.fetch(DisclosurePartition("sse", _NOW.date(), _NOW.date()))

    monkeypatch.setattr(CninfoMarketDisclosureSource, "fetch", fetch)
    monkeypatch.setattr(cli, "Main", main)
    original_digest, original_run = CompanyRawFiles.digest, SqliteConnection.run
    with pytest.raises(type(error)) as raised:
        company_pipeline_audit.MeasureCompanySync(configuration, report)
    assert raised.value is error
    assert CninfoMarketDisclosureSource.fetch is fetch
    assert CompanyRawFiles.digest is original_digest and SqliteConnection.run is original_run
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["cli_exit_code"] is None and payload["failure_category"] in {"runtime_error", "keyboard_interrupt"}
    assert not payload["database"]["completed"] and "private" not in report.read_text(encoding="utf-8")


@pytest.mark.parametrize("cli_fails", (False, True))
def test_report_write_failure_restores_hooks_and_does_not_replace_the_cli_error(tmp_path, monkeypatch, capsys, cli_fails):
    configuration, report = _paths(tmp_path)
    original = RuntimeError("private-original-cli-error")
    output_error = OSError("private-report-error")
    fetch, digest, run = CninfoMarketDisclosureSource.fetch, CompanyRawFiles.digest, SqliteConnection.run

    def main(argv):
        if cli_fails:
            raise original
        return 0

    def failed_report(*args, **kwargs):
        raise output_error

    monkeypatch.setattr(cli, "Main", main)
    monkeypatch.setattr(company_pipeline_audit, "AtomicWriteJson", failed_report)
    with pytest.raises(RuntimeError if cli_fails else OSError) as raised:
        company_pipeline_audit.MeasureCompanySync(configuration, report)
    assert raised.value is (original if cli_fails else output_error)
    assert CninfoMarketDisclosureSource.fetch is fetch and CompanyRawFiles.digest is digest
    assert SqliteConnection.run is run
    assert json.loads(report.read_text(encoding="utf-8"))["status"] == "running"
    error_output = capsys.readouterr().err
    assert "private" not in error_output
    assert ("report_write_failed" in error_output) is cli_fails


@pytest.mark.parametrize("kind", ("inside_runtime_project", "relative", "existing"))
def test_live_measurement_rejects_unsafe_report_before_calling_cli(tmp_path, monkeypatch, kind):
    configuration, report = _paths(tmp_path)
    if kind == "inside_runtime_project":
        report = configuration.parent.parent / "report.json"
    elif kind == "relative":
        report = Path("report.json")
    else:
        report.write_text("keep", encoding="utf-8")
    calls = []
    monkeypatch.setattr(cli, "Main", lambda argv: calls.append(argv))
    with pytest.raises((ValueError, FileExistsError)):
        company_pipeline_audit.MeasureCompanySync(configuration, report)
    assert calls == []
    if kind == "existing":
        assert report.read_text(encoding="utf-8") == "keep"


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
