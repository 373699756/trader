"""Same-document serial/parallel probe with real PDFs and disposable company indexes."""

from __future__ import annotations

from scripts.core.paths import PROJECT_ROOT as TOOL_ROOT

from trader.infra.sqlite_policy import SqliteIoPolicy

import hashlib
import json
import math
import sqlite3
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from functools import partial
from pathlib import Path
from statistics import median
from threading import Event, Lock, Thread, current_thread
from typing import ParamSpec, TypeVar
from unittest.mock import patch
from zoneinfo import ZoneInfo

from trader.bootstrap import OpenExternalHttp
from trader.download.application.company_evidence import CompanyEvidenceDependencies, SyncCompanyEvidence
from trader.download.domain.company_event_extraction import CompanyDocumentNeedsReview
from trader.download.domain.company_evidence import (
    BuildCompanyDocumentPolicies,
    CompanyEvidenceRequest,
    CompanyDocumentExtraction,
    CompanyDocumentFailure,
    CompanyDocumentFailureRules,
    CompanySourceFailure,
    Disclosure,
    DisclosurePage,
    DisclosurePartition,
    EvidenceMarket,
    EvidenceSyncProgress,
    Market,
    DocumentRoute,
    StructuredCompanyPage,
    StructuredCompanyQuery,
)
from trader.download.infra.company_disclosure_source import CninfoMarketDisclosureSource
from trader.download.infra.company_document_files import CompanyDocumentFiles
from trader.download.infra.company_document_ocr import CompanyOcrEngine, DetectCompanyOcr
from trader.download.infra.company_document_parser import DetectCompanyText
from trader.download.infra import company_evidence_index
from trader.download.infra.company_evidence_index import SQLiteCompanyEvidenceIndex
from trader.download.infra.company_fact_inputs import DecodeCompanyDisclosure
from trader.download.infra.company_facts_reader import SQLiteCompanyEvidenceReader
from trader.download.infra.company_facts_index import SQLiteCompanyFactsIndex
from trader.download.infra.company_raw_files import CompanyLocalDocument, CompanyRawFiles
from trader.download.infra.company_http import CompanyRequestPacer
from trader.download.infra.company_structured_source import EastmoneyCompanySource
from trader.download.infra.download_settings import LoadCompanySyncConfiguration
from trader.infra.sqlite_connection import SqliteConnectionOptions, SqliteSession
from trader.recommendation.infra.settings.loading import LoadStrategySettings
from trader.infra.settings.parser import InferProjectRoot
from trader.infra.atomic_files import AtomicWriteJson
from trader.infra.http_contracts import (
    HttpCancelled,
    HttpConnectionError,
    HttpError,
    HttpResponseTooLarge,
    HttpStatusError,
    HttpTimeout,
)
from trader.infra.workers import BoundedExecutor

_Parameters = ParamSpec("_Parameters")
_Value = TypeVar("_Value")
_ROOT = TOOL_ROOT
# The OCR handoff is an internal control signal, absent from persisted retry
# rules. Preserve its known identity instead of hiding it as an unknown error.
_DOCUMENT_FAILURES = frozenset(failure for failure, _route, _transient in CompanyDocumentFailureRules()) | {
    "disclosure_document_ocr_required",
}
_SOURCE_FAILURES = frozenset({"access_denied", "rate_limited", "http_error", "timeout", "schema_invalid"})
_RUNNING_REPORT_INTERVAL_SECONDS = 30.0
_MAX_PENDING_STARTS_PER_OPERATION = 256
_OPERATIONS = (
    "directory_fetch", "directory_request", "structured_financial_fetch", "structured_pledge_fetch",
    "structured_unlock_fetch", "annual_income_fetch", "pledge_day", "news_fetch", "download_dispatch", "download_transfer",
    "extract_standard", "extract_ocr", "extract_limits", "analyze_standard", "analyze_ocr", "analyze_limits",
    "ocr_execute", "raw_usage", "raw_size_scan", "raw_digest", "pacer_wait", "collection_publish", "facts_publish",
    "collection_initialize", "publication_prepare", "facts_inputs", "facts_compile", "facts_seal", "material_cleanup",
    "raw_reservation_scan",
    "text_read", "document_commit", "document_stage", "structured_commit", "publication_recovery",
)


@dataclass
class _OperationCounts:
    calls: int = 0
    wall_seconds: float = 0.0
    thread_cpu_seconds: float = 0.0
    failures: dict[str, int] = field(default_factory=dict)
    pending_starts: list[float] = field(default_factory=list)
    untracked_calls: int = 0


class _CompanySyncMeasurement:
    """Keep fixed operation counters; never retain call arguments or exceptions."""

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        snapshot_interval_seconds: float = _RUNNING_REPORT_INTERVAL_SECONDS,
    ) -> None:
        if not math.isfinite(snapshot_interval_seconds) or snapshot_interval_seconds <= 0:
            raise ValueError("company_sync_measurement_snapshot_interval_invalid")
        self._lock = Lock()
        self._snapshot_io_lock = Lock()
        self._counts = {name: _OperationCounts() for name in _OPERATIONS}
        self._monotonic = monotonic
        self._snapshot_interval_seconds = snapshot_interval_seconds
        self._snapshot_writer: Callable[[], None] | None = None
        self._snapshot_stop = Event()
        self._snapshot_thread: Thread | None = None
        self._snapshot_configured = False
        self._snapshot_failed = False
        self._closed = False

    def set_snapshot_writer(self, writer: Callable[[], None]) -> None:
        start_failed = False
        with self._lock:
            if self._closed:
                raise RuntimeError("company_sync_measurement_closed")
            if self._snapshot_configured:
                raise RuntimeError("company_sync_measurement_writer_already_set")
            self._snapshot_configured = True
            self._snapshot_writer = writer
            worker = Thread(target=self._snapshot_loop, name="company-measurement-heartbeat", daemon=False)
            self._snapshot_thread = worker
            try:
                worker.start()
            except RuntimeError:
                self._snapshot_writer = None
                self._snapshot_failed = True
                self._snapshot_stop.set()
                start_failed = True
            except (KeyboardInterrupt, SystemExit):
                self._snapshot_writer = None
                self._snapshot_stop.set()
                raise
        if start_failed:
            self._snapshot_notice("running_report_thread_start_failed")

    @property
    def snapshot_interval_seconds(self) -> float:
        return self._snapshot_interval_seconds

    @property
    def snapshot_failed(self) -> bool:
        with self._lock:
            return self._snapshot_failed

    def close_snapshots(self) -> None:
        with self._lock:
            worker = self._snapshot_thread
            if worker is current_thread():
                raise RuntimeError("company_sync_measurement_close_from_heartbeat")
            self._closed = True
            self._snapshot_writer = None
        self._snapshot_stop.set()
        # An interrupted start may leave a Thread object without a native thread.
        # Its stop flag is already set, so a delayed bootstrap cannot publish.
        if worker is not None and worker.ident is not None:
            worker.join()
        # A claimed writer rechecks _closed after taking this lock. Waiting for
        # the current writer ensures the final report always follows running.
        with self._snapshot_io_lock:
            pass

    def _snapshot_loop(self) -> None:
        while not self._snapshot_stop.wait(self._snapshot_interval_seconds):
            with self._lock:
                if self._closed or self._snapshot_failed:
                    return
                writer = self._snapshot_writer
            if writer is None:
                return
            self._publish_running(writer)

    @contextmanager
    def operation(self, name: str) -> Iterator[None]:
        if name not in self._counts:
            raise ValueError("company_sync_measurement_operation_invalid")
        entered = self._monotonic()
        with self._lock:
            count = self._counts[name]
            tracked = len(count.pending_starts) < _MAX_PENDING_STARTS_PER_OPERATION
            if tracked:
                count.pending_starts.append(entered)
            else:
                count.untracked_calls += 1
        started, cpu = time.perf_counter(), time.thread_time()
        failure: str | None = None
        try:
            yield
        except BaseException as exc:
            # Observation must preserve cancellation and the original exception.
            failure = _failure_category(exc)
            raise
        finally:
            wall, thread_cpu = time.perf_counter() - started, time.thread_time() - cpu
            with self._lock:
                count = self._counts[name]
                if tracked:
                    count.pending_starts.remove(entered)
                else:
                    count.untracked_calls -= 1
                count.calls += 1
                count.wall_seconds += wall
                count.thread_cpu_seconds += thread_cpu
                if failure is not None:
                    if failure not in count.failures and len(count.failures) >= 31:
                        failure = "other"
                    count.failures[failure] = count.failures.get(failure, 0) + 1

    def _publish_running(self, writer: Callable[[], None]) -> None:
        with self._snapshot_io_lock:
            with self._lock:
                if self._closed or self._snapshot_failed:
                    return
            try:
                writer()
            except (OSError, ValueError, TypeError, RuntimeError):
                with self._lock:
                    self._snapshot_failed = True
                    self._snapshot_writer = None
                self._snapshot_stop.set()
                self._snapshot_notice("running_report_write_failed")

    @staticmethod
    def _snapshot_notice(reason: str) -> None:
        try:
            print(f"company live-sync: {reason}; 后续运行中快照已关闭。", file=sys.stderr)
        except OSError:
            pass  # Diagnostic output must not replace the business result.

    def wrap(self, name: str, operation: Callable[_Parameters, _Value]) -> Callable[_Parameters, _Value]:
        def measured(*args: _Parameters.args, **kwargs: _Parameters.kwargs) -> _Value:
            with self.operation(name):
                return operation(*args, **kwargs)

        return measured

    def report(self) -> list[dict[str, object]]:
        now = self._monotonic()
        with self._lock:
            return [
                {
                    "operation": name,
                    "calls": count.calls,
                    "wall_seconds": count.wall_seconds,
                    "thread_cpu_seconds": count.thread_cpu_seconds,
                    "inflight_calls": len(count.pending_starts) + count.untracked_calls,
                    "oldest_inflight_seconds": None if count.untracked_calls else max(
                        (max(0.0, now - started) for started in count.pending_starts), default=0.0
                    ),
                    "inflight_duration_complete": count.untracked_calls == 0,
                    "untracked_inflight_calls": count.untracked_calls,
                    "failures": [
                        {"category": category, "calls": calls} for category, calls in sorted(count.failures.items())
                    ],
                }
                for name, count in self._counts.items()
            ]


def _failure_category(exc: BaseException) -> str:
    if isinstance(exc, KeyboardInterrupt):
        return "keyboard_interrupt"
    if isinstance(exc, SystemExit):
        return "system_exit"
    if isinstance(exc, CompanyDocumentFailure):
        return exc.reason if exc.reason in _DOCUMENT_FAILURES else "document_other"
    if isinstance(exc, CompanySourceFailure):
        return exc.category if exc.category in _SOURCE_FAILURES else "source_other"
    for kind, category in (
        (HttpCancelled, "cancelled"), (HttpTimeout, "timeout"), (TimeoutError, "timeout"),
        (HttpConnectionError, "connection_error"),
        (HttpResponseTooLarge, "response_too_large"), (HttpStatusError, "http_status_error"),
        (HttpError, "http_error"), (sqlite3.Error, "sqlite_error"), (InterruptedError, "interrupted"), (OSError, "io_error"),
        (ValueError, "validation_error"), (RuntimeError, "runtime_error"),
    ):
        if isinstance(exc, kind):
            return category
    return "unexpected_exception"


def _observe_company_operations(stack: ExitStack, measurement: _CompanySyncMeasurement) -> None:
    fetch = EastmoneyCompanySource.fetch
    extract = CninfoMarketDisclosureSource.extract_document
    analyze = CninfoMarketDisclosureSource._analyze_document

    def structured(
        source: EastmoneyCompanySource, query: StructuredCompanyQuery, *, observed_at: datetime
    ) -> StructuredCompanyPage:
        with measurement.operation(f"structured_{query.component}_fetch"):
            return fetch(source, query, observed_at=observed_at)

    def extracted(
        source: CninfoMarketDisclosureSource, item: Disclosure, *, route: DocumentRoute = "standard"
    ) -> CompanyDocumentExtraction:
        with measurement.operation(f"extract_{route}"):
            return extract(source, item, route=route)

    def analyzed(
        source: CninfoMarketDisclosureSource, local: CompanyLocalDocument, route: DocumentRoute
    ) -> CompanyDocumentExtraction:
        with measurement.operation(f"analyze_{route}"):
            return analyze(source, local, route)

    for owner, name, replacement in (
        (CninfoMarketDisclosureSource, "fetch", measurement.wrap("directory_fetch", CninfoMarketDisclosureSource.fetch)),
        (CninfoMarketDisclosureSource, "_fetch_page", measurement.wrap("directory_request", CninfoMarketDisclosureSource._fetch_page)),
        (EastmoneyCompanySource, "fetch", structured),
        (EastmoneyCompanySource, "pledge_day", measurement.wrap("pledge_day", EastmoneyCompanySource.pledge_day)),
        (EastmoneyCompanySource, "annual_income", measurement.wrap("annual_income_fetch", EastmoneyCompanySource.annual_income)),
        (EastmoneyCompanySource, "news", measurement.wrap("news_fetch", EastmoneyCompanySource.news)),
        (CninfoMarketDisclosureSource, "download_document", measurement.wrap("download_dispatch", CninfoMarketDisclosureSource.download_document)),
        (CninfoMarketDisclosureSource, "_transfer_document", measurement.wrap("download_transfer", CninfoMarketDisclosureSource._transfer_document)),
        (CninfoMarketDisclosureSource, "extract_document", extracted),
        (CninfoMarketDisclosureSource, "_analyze_document", analyzed),
        (CninfoMarketDisclosureSource, "_ocr", measurement.wrap("ocr_execute", CninfoMarketDisclosureSource._ocr)),
        (CompanyRawFiles, "usage", measurement.wrap("raw_usage", CompanyRawFiles.usage)),
        (CompanyRawFiles, "_size", staticmethod(measurement.wrap("raw_size_scan", CompanyRawFiles._size))),
        (CompanyRawFiles, "digest", measurement.wrap("raw_digest", CompanyRawFiles.digest)),
        (CompanyRawFiles, "_reservation_usage", measurement.wrap("raw_reservation_scan", CompanyRawFiles._reservation_usage)),
        (CompanyDocumentFiles, "read", measurement.wrap("text_read", CompanyDocumentFiles.read)),
        (SQLiteCompanyEvidenceIndex, "commit_document", measurement.wrap("document_commit", SQLiteCompanyEvidenceIndex.commit_document)),
        (SQLiteCompanyEvidenceIndex, "document_stage", measurement.wrap("document_stage", SQLiteCompanyEvidenceIndex.document_stage)),
        (SQLiteCompanyEvidenceIndex, "commit_structured", measurement.wrap("structured_commit", SQLiteCompanyEvidenceIndex.commit_structured)),
        (SQLiteCompanyEvidenceIndex, "_repair_pending_files", measurement.wrap("publication_recovery", SQLiteCompanyEvidenceIndex._repair_pending_files)),
        (CompanyRequestPacer, "wait", measurement.wrap("pacer_wait", CompanyRequestPacer.wait)),
        (SQLiteCompanyEvidenceIndex, "initialize", measurement.wrap("collection_initialize", SQLiteCompanyEvidenceIndex.initialize)),
        (SQLiteCompanyEvidenceIndex, "prepare_publication", measurement.wrap("publication_prepare", SQLiteCompanyEvidenceIndex.prepare_publication)),
        (company_evidence_index, "ReadCompanySnapshotRows", measurement.wrap("facts_inputs", company_evidence_index.ReadCompanySnapshotRows)),
        (company_evidence_index, "CompileCompanyFacts", measurement.wrap("facts_compile", company_evidence_index.CompileCompanyFacts)),
        (SQLiteCompanyEvidenceIndex, "publish", measurement.wrap("collection_publish", SQLiteCompanyEvidenceIndex.publish)),
        (SQLiteCompanyFactsIndex, "publish", measurement.wrap("facts_publish", SQLiteCompanyFactsIndex.publish)),
        (SQLiteCompanyFactsIndex, "publish_file", measurement.wrap("facts_seal", SQLiteCompanyFactsIndex.publish_file)),
        (SQLiteCompanyEvidenceIndex, "_release_files", measurement.wrap("material_cleanup", SQLiteCompanyEvidenceIndex._release_files)),
    ):
        stack.enter_context(patch.object(owner, name, replacement))


def MeasureCompanySync(runtime_config: Path, report_path: Path, *, fill_missing: bool = False) -> int:
    """Explicitly run the real CLI against current company data and save bounded measurements.

    This mode writes current data. It neither constructs clients nor imposes the
    sampled audit budget; the normal CLI owns cancellation and all resources.
    """
    from scripts.infra.database.database_measurement import DatabaseMeasurement
    from trader.entrypoints import cli

    configuration = runtime_config.expanduser().resolve(strict=True)
    destination = report_path.expanduser()
    if not destination.is_absolute() or destination.is_symlink():
        raise ValueError("company_live_sync_report_must_be_external_absolute")
    destination = destination.resolve()
    if any(destination == root or root in destination.parents for root in (_ROOT, InferProjectRoot(configuration.parent))):
        raise ValueError("company_live_sync_report_must_be_external_absolute")
    destination.parent.mkdir(parents=True, exist_ok=True)
    configuration_hash = hashlib.sha256(configuration.read_bytes()).hexdigest()
    measurements = _CompanySyncMeasurement()
    database = DatabaseMeasurement(destination.parent, sample_wal=False)
    exit_code: int | None = None
    failure: str | None = None
    started, cpu = time.perf_counter(), time.process_time()

    def report_payload(*, running: bool) -> dict[str, object]:
        return {
            "schema_version": "company-live-sync-measurement",
            "status": "running" if running else "passed" if exit_code == 0 else "degraded" if (
                failure is None and exit_code in {1, 130} or failure == "keyboard_interrupt"
            ) else "failed",
            "cancelled": not running and (exit_code == 130 or failure == "keyboard_interrupt"),
            "writes_current_company_data": True,
            "isolated_sample": False,
            "status_basis": "cli_exit_code_only",
            "field_correctness_verified": False,
            "fill_missing": fill_missing,
            "runtime_configuration_sha256": configuration_hash,
            "cli_exit_code": None if running else exit_code,
            "failure_category": None if running else failure,
            "wall_seconds": time.perf_counter() - started,
            "process_cpu_seconds": time.process_time() - cpu,
            "operation_timing": "inclusive; recursive, nested and concurrent operations overlap and must not be summed as total wall time",
            "operation_counts": "completed method invocations; inflight counts and durations are separate; directory_request and download_transfer count physical attempts",
            "operation_boundaries": {
                "publication_prepare": "prepare_publication startup/resume work; may include publication",
                "facts_inputs": "ReadCompanySnapshotRows batch SQL input read and decode",
                "facts_compile": "CompileCompanyFacts only; event linking and other publication work remain outside",
                "facts_seal": "publish_file call including its no-file branch; not a pure hash or fsync duration",
                "raw_reservation_scan": "candidate _reservation_usage call; not included in raw_usage or raw_size_scan",
            },
            "route_classification": "dispatch route; OCR fallback is also measured by ocr_execute",
            "thread_cpu_includes_child_processes": False,
            "coverage": "company adapters and publication; CLI total also includes shared reference preparation",
            "snapshot_interval_seconds": measurements.snapshot_interval_seconds,
            "snapshot_trigger": "periodic_timer",
            "snapshot_failed": measurements.snapshot_failed,
            "physical_io_includes_running_reports": True,
            "operations": measurements.report(),
            "database": database.running_snapshot() if running else database.result,
            "database_statistics_final": not running,
        }

    with destination.open("x", encoding="utf-8") as stream:
        json.dump(report_payload(running=True), stream, ensure_ascii=False, indent=2, allow_nan=False)
    print("company live-sync: 将通过现有 CLI 写入当前 company 数据；不使用隔离副本或采样预算。", file=sys.stderr)
    try:
        measurements.set_snapshot_writer(lambda: AtomicWriteJson(destination, report_payload(running=True), indent=2))
        with ExitStack() as stack:
            _observe_company_operations(stack, measurements)
            stack.enter_context(database)
            arguments = ["--config", str(configuration), "sync", "company"]
            if fill_missing:
                arguments.append("fill")
            exit_code = cli.Main(arguments)
        return exit_code
    except BaseException as exc:
        failure = _failure_category(exc)
        raise
    finally:
        measurements.close_snapshots()
        try:
            AtomicWriteJson(destination, report_payload(running=False), indent=2)
        except OSError:
            if failure is None:
                raise
            # Preserve the CLI's original exception when reporting also fails.
            print("company live-sync: report_write_failed (io_error); 原 CLI 异常继续传播。", file=sys.stderr)


class _SampleIndex(SQLiteCompanyEvidenceIndex):
    def partitions(self, markets: Sequence[Market], observed_at: datetime) -> tuple[DisclosurePartition, ...]:
        return ()

    def component_due(self, component: str, scope: str, observed_at: datetime) -> bool:
        return False


def _sample(path: Path, *, sqlite_policy: SqliteIoPolicy) -> tuple[Disclosure, ...]:
    selected: dict[tuple[str, str], Disclosure] = {}
    with SqliteSession(path, SqliteConnectionOptions(read_only=True, timeout_seconds=3), sqlite_policy=sqlite_policy) as connection:
        rows = connection.run(
            "SELECT d.source,d.source_key,d.kind,d.title,d.published_at,d.url,d.record_hash,c.code "
            "FROM disclosures d JOIN disclosure_codes c ON c.disclosure_id=d.id "
            "WHERE d.id IN (SELECT id FROM disclosures WHERE kind='announcement' ORDER BY published_at DESC LIMIT 3000) "
            "ORDER BY d.published_at DESC,d.id,c.code"
        )
        for row in rows:
            if CompanyDocumentNeedsReview(row["title"]):
                item = DecodeCompanyDisclosure(row)
                key = item.source, item.identity
                if key not in selected and len(selected) == 12:
                    break
                prior = selected.get(key)
                selected[key] = item if prior is None else replace(prior, codes=(*prior.codes, *item.codes))
    return tuple(selected.values())


def _directory_sample(path: Path, now: datetime, *, sqlite_policy: SqliteIoPolicy) -> DisclosurePartition | None:
    with SqliteSession(path, SqliteConnectionOptions(read_only=True, timeout_seconds=3), sqlite_policy=sqlite_policy) as connection:
        row = connection.run(
            "SELECT market,start_date FROM partitions WHERE is_complete=1 AND start_date=end_date "
            "AND end_date<? AND expected_rows BETWEEN 31 AND 600 ORDER BY end_date DESC,market LIMIT 1",
            (now.date().isoformat(),),
        ).fetchone()
    return DisclosurePartition(row[0], date.fromisoformat(row[1]), date.fromisoformat(row[1])) if row is not None else None


def _probe_directory(source: CninfoMarketDisclosureSource, target: DisclosurePartition) -> dict[str, object]:
    identities: set[tuple[str, str, tuple[str, ...]]] = set()
    records: set[tuple[str, str, tuple[str, ...], str]] = set()
    pages = 0
    total: int | None = None
    try:
        for number in range(1, 22):
            page = source.fetch(replace(target, page=number))
            pages += 1
            if page.total > 600 or total is not None and page.total != total:
                raise ValueError("cninfo_pacing_sample_changed")
            total = page.total
            keys = {(item.source, item.identity, tuple(sorted(item.codes))) for item in page.rows}
            if len(keys) != len(page.rows) or identities.intersection(keys):
                raise ValueError("cninfo_duplicate_page_links")
            identities.update(keys)
            records.update((item.source, item.identity, tuple(sorted(item.codes)), item.digest) for item in page.rows)
            if not page.has_more:
                break
        else:
            raise ValueError("cninfo_audit_page_limit")
        if len(identities) != total:
            raise ValueError("cninfo_query_coverage_mismatch")
    except (OSError, RuntimeError, ValueError) as exc:
        return {"complete": False, "pages": pages, "rows": len(identities), "failure": _failure_category(exc)}
    return {"complete": True, "pages": pages, "rows": len(identities), "digest": hashlib.sha256(repr(sorted(records)).encode()).hexdigest()}


def AuditCompanyPipeline(
    path: Path,
    directory: Path,
    *,
    budget_seconds: float,
    sqlite_policy: SqliteIoPolicy,
    compare_pacing: bool = False,
    candidate_publication_interval: float | None = None,
    sample: tuple[Disclosure, ...] | None = None,
    observed_at: datetime | None = None,
) -> dict[str, object]:
    compare_io = candidate_publication_interval is not None
    if compare_io and (
        compare_pacing or candidate_publication_interval not in (5.0, 15.0, 30.0, 60.0)
    ):
        raise ValueError("company_pipeline_candidate_invalid")
    items = _sample(path, sqlite_policy=sqlite_policy) if sample is None else sample
    if not items:
        return {"schema_version": "company-resume", "status": "degraded", "findings": ["no_document_sample"]}
    codes = tuple(sorted({code for item in items for code in item.codes}))
    now = datetime.now(ZoneInfo("Asia/Shanghai")) if observed_at is None else observed_at
    if now.utcoffset() is None:
        raise ValueError("company_pipeline_time_invalid")
    target = _directory_sample(path, now, sqlite_policy=sqlite_policy) if compare_pacing else None
    if compare_pacing and target is None:
        return {"schema_version": "company-resume", "status": "degraded", "findings": ["no_directory_sample"]}
    configuration = LoadCompanySyncConfiguration(TOOL_ROOT)
    ocr_capability = DetectCompanyOcr(configuration.document_ocr_languages)
    text_capability = DetectCompanyText()
    policies = BuildCompanyDocumentPolicies(configuration, ocr_capability.identity, text_identity=text_capability.identity)
    event_policy = LoadStrategySettings(
        TOOL_ROOT / "config" / "strategy.json"
    ).company_scoring.events
    deadline = time.monotonic() + budget_seconds
    runs: list[dict[str, object]] = []
    signatures: list[str] = []
    document_signatures: list[str] = []
    payload_signatures: list[str] = []
    variants = ((2, True), (2, True)) if compare_io else ((2, False), (2, True)) if compare_pacing else ((1, False), (2, False))
    for number, (count, independent_hosts) in enumerate(variants, 1):
        candidate = compare_io and number == 2
        candidate_interval = configuration.publication_interval_seconds if candidate_publication_interval is None else candidate_publication_interval
        run_configuration = replace(
            configuration,
            document_workers=count,
            publication_interval_seconds=candidate_interval if candidate else configuration.publication_interval_seconds,
        )
        run_directory = directory / f"pipeline-{number}"
        database = run_directory / "collection.sqlite3"
        if database.exists():
            raise ValueError("company_probe_requires_fresh_directory")
        raw_files = CompanyRawFiles(run_directory, capacity=configuration.raw_capacity_bytes,
                                   analysis_reserve=configuration.raw_analysis_reserve_bytes,
                                   free_reserve=configuration.temporary_disk_reserve_bytes,
                                   maximum_document_bytes=configuration.extended_document_limits.max_bytes,
                                   scan_reservations_once=candidate)
        index = _SampleIndex(
            database,
            raw_files=raw_files,
            facts_path=directory / f"facts-{number}.sqlite3",
            documents=CompanyDocumentFiles(
                run_directory / "text",
                max_text_characters=configuration.extended_document_limits.max_text_characters,
                max_bytes=configuration.temporary_text_capacity_bytes,
                reserve_bytes=configuration.temporary_disk_reserve_bytes,
            ),
            event_policy=event_policy, sqlite_policy=sqlite_policy, batch_publication_reads=candidate,
        )
        index.initialize()
        for market, day in sorted({(EvidenceMarket(item.codes[0]), item.published_at.date()) for item in items}):
            # This is explicitly a sampled input fixture, never a coverage certificate.
            rows = tuple(
                item for item in items if EvidenceMarket(item.codes[0]) == market and item.published_at.date() == day
            )
            index.commit_page(DisclosurePartition(market, day, day), DisclosurePage(rows, len(rows), False), now)
        started = time.monotonic()
        first_publication: float | None = None
        attempts = 0
        publications = 0

        def progress(event: EvidenceSyncProgress, started: float = started) -> None:
            nonlocal first_publication, attempts, publications
            attempts = max(attempts, event.attempted_documents)
            if event.component == "publish" and event.status == "complete":
                publications += 1
            if event.component == "publish" and event.status == "complete" and first_publication is None:
                first_publication = time.monotonic() - started

        with ExitStack() as resources:
            sessions = tuple(resources.enter_context(OpenExternalHttp()) for _ in range(count))

            def cancelled() -> bool:
                return time.monotonic() >= deadline

            engine = CompanyOcrEngine(
                ocr_capability,
                timeout_seconds=configuration.document_ocr_timeout_seconds,
                page_timeout_seconds=configuration.document_ocr_page_timeout_seconds,
                max_pages=configuration.document_ocr_max_pages,
                dpi=configuration.document_ocr_dpi,
                cancel_requested=cancelled,
            )
            pacer = CompanyRequestPacer(configuration.request_interval_seconds, time.monotonic, time.sleep, cancelled)
            source_factory = partial(
                CninfoMarketDisclosureSource,
                    monotonic=time.monotonic,
                    sleeper=time.sleep,
                    timeout_seconds=configuration.source_timeout_seconds,
                    document_download_timeout_seconds=configuration.document_download_timeout_seconds,
                    document_parse_timeout_seconds=configuration.document_parse_timeout_seconds,
                    document_limits=configuration.document_limits,
                    extended_document_limits=configuration.extended_document_limits,
                    request_interval_seconds=configuration.request_interval_seconds,
                    cancel_requested=cancelled,
                    ocr_engine=engine,
                    document_policies=policies,
                    text_capability=text_capability,
                    raw_files=raw_files,
                    text_reserve_bytes=configuration.text_task_reserve_bytes,
                    ocr_reserve_bytes=configuration.ocr_task_reserve_bytes,
            )
            sources = tuple(source_factory(session, pacer=pacer) for session in sessions)
            directory_future = None
            if target is not None:
                directory_pacer = CompanyRequestPacer(
                    configuration.request_interval_seconds, time.monotonic, time.sleep, cancelled
                ) if independent_hosts else pacer
                directory_source = source_factory(resources.enter_context(OpenExternalHttp()), pacer=directory_pacer)
                directory_worker = BoundedExecutor(worker_count=1, queue_capacity=0, thread_name_prefix="company-probe-directory")
                directory_worker.start()
                resources.callback(directory_worker.stop, wait=True, cancel_futures=True)
                directory_future = directory_worker.submit(_probe_directory, directory_source, target)
                if directory_future is None:
                    raise RuntimeError("company_directory_probe_rejected")
            workers = BoundedExecutor(worker_count=count, queue_capacity=2, thread_name_prefix="company-probe")
            workers.start()
            resources.callback(workers.stop, wait=True, cancel_futures=True)
            text_workers = BoundedExecutor(worker_count=configuration.text_workers, queue_capacity=0, thread_name_prefix="company-probe-text")
            ocr_workers = BoundedExecutor(worker_count=configuration.ocr_workers, queue_capacity=0, thread_name_prefix="company-probe-ocr")
            text_workers.start()
            resources.callback(text_workers.stop, wait=True, cancel_futures=True)
            ocr_workers.start()
            resources.callback(ocr_workers.stop, wait=True, cancel_futures=True)
            dependencies = CompanyEvidenceDependencies(
                index,
                sources[0],
                EastmoneyCompanySource(
                    sessions[0],
                    monotonic=time.monotonic,
                    timeout_seconds=configuration.source_timeout_seconds,
                ),
                time.monotonic,
                progress,
                cancelled,
                document_sources=sources,
                document_executor=workers,
                text_executor=text_workers,
                ocr_executor=ocr_workers,
                ocr_identity=ocr_capability.identity,
                text_identity=text_capability.identity,
                coalesce_publications=candidate,
            )
            result = SyncCompanyEvidence(
                dependencies,
                CompanyEvidenceRequest(
                    codes,
                    now,
                    run_configuration,
                    event_policy,
                ),
            )
            directory_result = directory_future.result() if directory_future is not None else None
            elapsed = time.monotonic() - started
            first_attempts = attempts
            first_publications = publications
            attempts = 0
            resumed = SyncCompanyEvidence(
                dependencies,
                CompanyEvidenceRequest(codes, now, run_configuration, event_policy),
            )
        facts = SQLiteCompanyEvidenceReader(index.facts_path, event_policy=event_policy, sqlite_policy=sqlite_policy).read(codes)
        # Compare business facts; observation timestamps naturally differ by worker count.
        comparable = tuple(
            replace(
                row,
                published_at=now,
                review_reasons=tuple(replace(reason, detected_at=now) for reason in row.review_reasons),
            )
            for row in facts
        )
        signature = hashlib.sha256(repr(comparable).encode()).hexdigest()
        signatures.append(signature)
        with SqliteSession(database, SqliteConnectionOptions(read_only=True), sqlite_policy=sqlite_policy) as connection:
            documents = tuple(tuple(row) for row in connection.run(
                "SELECT d.source,d.source_key,d.record_hash,d.body_checked,d.body_hash,e.payload_hash "
                "FROM disclosures d LEFT JOIN document_extractions e ON e.disclosure_id=d.id "
                "AND e.record_hash=d.record_hash WHERE d.kind='announcement' ORDER BY d.source,d.source_key"
            ))
            failure_counts = [
                {"failure": row[0], "documents": row[1]}
                for row in connection.run(
                    "SELECT r.failure,COUNT(*) FROM document_retries r JOIN disclosures d ON d.id=r.disclosure_id "
                    "AND d.record_hash=r.record_hash GROUP BY r.failure ORDER BY r.failure"
                )
            ]
            extraction_methods = [
                {"method": row[0], "documents": row[1]}
                for row in connection.run(
                    "SELECT e.method,COUNT(*) FROM document_extractions e JOIN disclosures d ON d.id=e.disclosure_id "
                    "AND d.record_hash=e.record_hash GROUP BY e.method ORDER BY e.method"
                )
            ]
        document_signature = hashlib.sha256(repr(tuple(row[:5] for row in documents)).encode()).hexdigest()
        payload_signature = hashlib.sha256(repr(tuple((*row[:3], row[5]) for row in documents)).encode()).hexdigest()
        document_signatures.append(document_signature)
        payload_signatures.append(payload_signature)
        extracted = sum(row[3] == 1 and row[4] is not None and row[5] is not None for row in documents)
        runs.append(
            {
                "workers": count,
                "io_candidate": candidate,
                "publication_interval_seconds": run_configuration.publication_interval_seconds,
                "directory_prefetch_measured": False,
                "pacing": "independent_hosts" if independent_hosts else "shared",
                "directory": directory_result,
                "sample_documents": len(items),
                "attempts": first_attempts,
                "pending_documents": result.pending_documents,
                "published_codes": result.published_codes,
                "seconds": round(elapsed, 3),
                "first_publication_seconds": first_publication,
                "publication_calls": first_publications,
                "resume_changed_rows": resumed.changed_rows,
                "resume_pending_documents": resumed.pending_documents,
                "resume_attempts": attempts,
                "failures": result.failures,
                "facts_digest": signature,
                "extracted_documents": extracted,
                "pending_ocr_documents": result.ocr_documents,
                "unextractable_documents": result.unextractable_documents,
                "document_failures": failure_counts,
                "document_extractions": extraction_methods,
                "documents_digest": document_signature,
                "payloads_digest": payload_signature,
            }
        )
        if time.monotonic() >= deadline:
            break
    equivalent = len(signatures) == 2 and signatures[0] == signatures[1]
    equivalent_documents = len(document_signatures) == 2 and document_signatures[0] == document_signatures[1]
    equivalent_payloads = len(payload_signatures) == 2 and payload_signatures[0] == payload_signatures[1]
    directories = [row["directory"] for row in runs]
    equivalent_directory = not compare_pacing or (
        len(directories) == 2 and all(isinstance(row, dict) and row.get("complete") for row in directories)
        and directories[0] == directories[1]
    )
    passed = equivalent and equivalent_documents and equivalent_payloads and equivalent_directory and all(
        row["pending_documents"] == 0
        and row["extracted_documents"] == len(items)
        and row["pending_ocr_documents"] == row["unextractable_documents"] == 0
        and not row["failures"]
        and row["attempts"] == len(items)
        and row["resume_changed_rows"] == 0
        and row["resume_attempts"] == 0
        and row["published_codes"] == len(codes)
        for row in runs
    )
    return {
        "schema_version": "company-resume",
        "status": "passed" if passed else "degraded",
        "summary": {"pipeline_audit": {"isolated_sample": True, "equivalent_facts": equivalent,
                                       "compare_pacing": compare_pacing, "equivalent_directory": equivalent_directory if compare_pacing else None,
                                       "compare_io": compare_io,
                                       "input_sample_digest": hashlib.sha256(repr(items).encode()).hexdigest(),
                                       "equivalent_documents": equivalent_documents,
                                       "equivalent_payloads": equivalent_payloads, "runs": runs}},
        "findings": [] if passed else ["company_pipeline_probe_incomplete"],
    }


@dataclass(frozen=True)
class CompanyPublicationTrial:
    interval_seconds: float
    wall_seconds: float
    input_digest: str
    facts_digest: str
    payloads_digest: str
    passed: bool


def SelectCompanyPublicationInterval(trials: tuple[CompanyPublicationTrial, ...]) -> float | None:
    """Require five valid, identical-input observations for every interval.

    This selects a measured candidate, never enables it. Prefer the shortest
    interval within five percent of the fastest median total duration.
    """
    intervals = (5.0, 15.0, 30.0, 60.0)
    if any(
        trial.interval_seconds not in intervals
        or not math.isfinite(trial.wall_seconds) or trial.wall_seconds <= 0
        for trial in trials
    ):
        raise ValueError("company_publication_trial_invalid")
    if not trials or any(not trial.passed for trial in trials):
        return None
    identities = {(trial.input_digest, trial.facts_digest, trial.payloads_digest) for trial in trials}
    if len(identities) != 1 or any(not digest for identity in identities for digest in identity):
        return None
    medians = []
    for interval in intervals:
        values = tuple(trial.wall_seconds for trial in trials if trial.interval_seconds == interval)
        if len(values) < 5:
            return None
        medians.append((interval, median(values)))
    fastest = min(seconds for _interval, seconds in medians)
    return min(interval for interval, seconds in medians if seconds <= fastest * 1.05)


def AuditCompanyIoCandidates(
    path: Path,
    directory: Path,
    *,
    budget_seconds: float,
    sqlite_policy: SqliteIoPolicy,
) -> dict[str, object]:
    """Prepare paired diagnostics for C's idle HDD measurement window.

    This mode downloads the same bounded PDF sample into isolated directories.
    It never changes production configuration or certifies full-sync coverage.
    Directory prefetch requires a separate directory workload measurement.
    """
    if not math.isfinite(budget_seconds) or not 30 <= budget_seconds <= 10800:
        raise ValueError("company_io_candidate_budget_invalid")
    if not directory.is_absolute() or directory == _ROOT or _ROOT in directory.parents:
        raise ValueError("company_io_candidate_directory_invalid")
    configuration = LoadCompanySyncConfiguration(TOOL_ROOT)
    if configuration.publication_interval_seconds != 5.0:
        raise ValueError("company_io_candidate_baseline_requires_five_seconds")
    sample = _sample(path, sqlite_policy=sqlite_policy)
    observed_at = datetime.now(ZoneInfo("Asia/Shanghai"))
    deadline = time.monotonic() + budget_seconds
    pairs: list[dict[str, object]] = []
    trials: list[CompanyPublicationTrial] = []
    for round_number in range(1, 6):
        for interval in (15.0, 30.0, 60.0):
            remaining = deadline - time.monotonic()
            if remaining < 30:
                break
            pair = AuditCompanyPipeline(
                path,
                directory / f"round-{round_number}-interval-{int(interval)}",
                budget_seconds=remaining,
                sqlite_policy=sqlite_policy,
                candidate_publication_interval=interval,
                sample=sample,
                observed_at=observed_at,
            )
            pairs.append(pair)
            summary = pair.get("summary")
            pipeline = summary.get("pipeline_audit") if isinstance(summary, dict) else None
            if not isinstance(pipeline, dict) or not isinstance(pipeline.get("runs"), list):
                continue
            for run in pipeline["runs"]:
                if not isinstance(run, dict):
                    raise ValueError("company_io_candidate_report_invalid")
                trials.append(CompanyPublicationTrial(
                    float(run["publication_interval_seconds"]), float(run["seconds"]),
                    str(pipeline["input_sample_digest"]), str(run["facts_digest"]), str(run["payloads_digest"]),
                    pair["status"] == "passed",
                ))
    selected = SelectCompanyPublicationInterval(tuple(trials)) if len(pairs) == 15 else None
    return {
        "schema_version": "company-resume",
        "status": "passed" if selected is not None else "degraded",
        "summary": {"io_candidates": {
            "isolated_sample": True, "rounds_required": 5, "pairs_completed": len(pairs),
            "selected_interval_seconds": selected, "production_enabled": False,
            "performance_accepted": False, "full_sync_verified": False,
            "directory_prefetch_measured": False, "pairs": pairs,
        }},
        "findings": [] if selected is not None else ["company_io_candidate_comparison_incomplete"],
    }
