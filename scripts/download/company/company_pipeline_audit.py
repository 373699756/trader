"""Same-document serial/parallel probe with real PDFs and disposable company indexes."""

from __future__ import annotations

from scripts.download.company.output_paths import RequireIsolatedReplay
from trader.download.infra.company_material_index import SQLiteCompanyMaterialIndex
from trader.download.infra.company_normalized_index import CompanyNormalizationBaseline, SQLiteCompanyNormalizedIndex
from trader.download.infra.company_facts_index import CompanyInputReceipt, CompanyPublicationIdentity
from trader.download.infra.company_field_publication import CompanyFieldProofRulesHash, PrepareCompanyFieldResults
from trader.download.domain.company_evidence import CompanyDocument, CompanyDocumentRouteForFailure, CompanyEvidenceInput
from trader.download.domain.company_facts import CompileCompanyFacts
from trader.download.domain.company_field_plan import BuildCompanyFieldResults
from trader.download.domain.company_field_results import CompanyFieldConclusion, CompanyFieldConclusionState, CompanyFieldName
from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.infra.http_contracts import HttpTransport
from trader.infra.sqlite_connection import SqliteTransaction
from typing import cast
from datetime import timedelta
from scripts.core.paths import PROJECT_ROOT as TOOL_ROOT

from trader.infra.sqlite_policy import SqliteIoPolicy

import hashlib
import math
import sqlite3
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path
from threading import Event, Lock, Thread, current_thread
from typing import ParamSpec, TypeVar
from unittest.mock import patch
from zoneinfo import ZoneInfo

from trader.bootstrap import OpenExternalHttp
from trader.download.domain.company_event_extraction import CompanyDocumentNeedsReview
from trader.download.domain.company_evidence import BuildCompanyDocumentPolicies, CompanyDocumentExtraction, CompanyDocumentFailure, CompanyDocumentFailureRules, CompanySourceFailure, Disclosure, DisclosurePartition
from trader.download.infra.company_disclosure_source import CninfoMarketDisclosureSource
from trader.download.infra.company_document_files import CompanyDocumentFiles
from trader.download.infra.company_document_ocr import CompanyOcrEngine, DetectCompanyOcr
from trader.download.infra.company_document_parser import DetectCompanyText
from trader.download.infra.company_fact_inputs import DecodeCompanyDisclosure
from trader.download.infra.company_facts_reader import SQLiteCompanyEvidenceReader
from trader.download.infra.company_facts_index import SQLiteCompanyFactsIndex
from trader.download.infra.company_raw_files import CompanyRawFiles
from trader.download.infra.company_http import CompanyRequestPacer
from trader.download.domain.company_sync import CompanySourceCancelled
from trader.download.infra.download_settings import LoadCompanySyncConfiguration
from trader.infra.sqlite_connection import SqliteConnectionOptions, SqliteSession
from trader.recommendation.infra.settings.loading import LoadStrategySettings
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
        (CompanySourceCancelled, "cancelled"), (HttpCancelled, "cancelled"), (HttpTimeout, "timeout"), (TimeoutError, "timeout"),
        (HttpConnectionError, "connection_error"),
        (HttpResponseTooLarge, "response_too_large"), (HttpStatusError, "http_status_error"),
        (HttpError, "http_error"), (sqlite3.Error, "sqlite_error"), (InterruptedError, "interrupted"), (OSError, "io_error"),
        (ValueError, "validation_error"), (RuntimeError, "runtime_error"),
    ):
        if isinstance(exc, kind):
            return category
    return "unexpected_exception"


def _observe_company_operations(stack: ExitStack, measurement: _CompanySyncMeasurement) -> None:
    for owner, name, operation in (
        (CninfoMarketDisclosureSource, "_fetch_page", "directory_request"),
        (CninfoMarketDisclosureSource, "download_document", "download_dispatch"),
        (CninfoMarketDisclosureSource, "_transfer_document", "download_transfer"),
        (CninfoMarketDisclosureSource, "extract_document", "extract_standard"),
        (CninfoMarketDisclosureSource, "_ocr", "ocr_execute"),
        (CompanyRawFiles, "usage", "raw_usage"),
        (CompanyRawFiles, "_reservation_usage", "raw_reservation_scan"),
        (CompanyRawFiles, "digest", "raw_digest"),
        (CompanyDocumentFiles, "read", "text_read"),
        (SQLiteCompanyMaterialIndex, "commit_document", "document_commit"),
        (SQLiteCompanyMaterialIndex, "document_stage", "document_stage"),
        (SQLiteCompanyMaterialIndex, "initialize", "collection_initialize"),
        (CompanyRequestPacer, "wait", "pacer_wait"),
        (SQLiteCompanyFactsIndex, "publish", "facts_publish"),
        (SQLiteCompanyFactsIndex, "publish_file", "facts_seal"),
    ):
        stack.enter_context(patch.object(owner, name, measurement.wrap(operation, getattr(owner, name))))
    stack.enter_context(patch.object(CompanyRawFiles, "_size", staticmethod(
        measurement.wrap("raw_size_scan", CompanyRawFiles._size),
    )))


def MeasureCompanySync(runtime_config: Path, report_path: Path) -> int:
    """Ordinary sync shares the source admission gate, including its measurement entry."""
    raise ValueError("company_structured_source_admission_pending")


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
    sample: tuple[Disclosure, ...] | None = None,
    observed_at: datetime | None = None,
) -> dict[str, object]:
    """Research one explicit bounded PDF set, then prepare fields and publish typed inputs.

    Directory coverage and supplier financial/risk adoption are never inferred
    from this document sample. No default job, per-stock replenishment or
    recursive publication runs here.
    """
    if not math.isfinite(budget_seconds) or not 30 <= budget_seconds <= 10800:
        raise ValueError("company_pipeline_budget_invalid")
    RequireIsolatedReplay(directory, (_ROOT, path.parent))
    if directory.exists() and any(directory.iterdir()):
        raise ValueError("company_probe_requires_fresh_directory")
    items = _sample(path, sqlite_policy=sqlite_policy) if sample is None else sample
    if not 1 <= len(items) <= 12 or len({(item.source, item.identity) for item in items}) != len(items):
        raise ValueError("company_pipeline_sample_invalid")
    now = datetime.now(ZoneInfo("Asia/Shanghai")) if observed_at is None else observed_at
    if now.utcoffset() is None or any(item.published_at > now for item in items):
        raise ValueError("company_pipeline_time_invalid")
    target = _directory_sample(path, now, sqlite_policy=sqlite_policy) if compare_pacing else None
    if compare_pacing and target is None:
        return { "status": "degraded", "findings": ["no_directory_sample"]}
    configuration = LoadCompanySyncConfiguration(TOOL_ROOT)
    ocr = DetectCompanyOcr(configuration.document_ocr_languages)
    text = DetectCompanyText()
    policies = BuildCompanyDocumentPolicies(configuration, ocr.identity, text_identity=text.identity)
    event_policy = LoadStrategySettings(TOOL_ROOT / "config/strategy.json").company_scoring.events
    deadline = time.monotonic() + budget_seconds
    def cancelled() -> bool:
        return time.monotonic() >= deadline
    def check() -> None:
        if cancelled():
            raise TimeoutError("company_pipeline_deadline_exceeded")
    runs: list[dict[str, object]] = []
    variants = ((2, False), (2, True)) if compare_pacing else ((1, False), (2, False))
    for number, (count, independent_hosts) in enumerate(variants, 1):
        check()
        run_directory = directory / f"pipeline-{number}"
        raw_files = CompanyRawFiles(run_directory / "raw", capacity=configuration.raw_capacity_bytes,
            analysis_reserve=configuration.raw_analysis_reserve_bytes, free_reserve=configuration.temporary_disk_reserve_bytes,
            maximum_document_bytes=configuration.extended_document_limits.max_bytes)
        documents = CompanyDocumentFiles(run_directory / "raw" / "text",
            max_text_characters=configuration.extended_document_limits.max_text_characters,
            max_bytes=configuration.temporary_text_capacity_bytes, reserve_bytes=configuration.temporary_disk_reserve_bytes)
        writer = SQLiteCompanyFactsIndex(run_directory / "company_published.sqlite3", event_policy=event_policy, sqlite_policy=sqlite_policy)
        sources_path = run_directory / "company_sources.sqlite3"
        normalized = SQLiteCompanyNormalizedIndex(sources_path, writer,
            event_policy=event_policy, sqlite_policy=sqlite_policy, clean_material=lambda: None, cancel_requested=cancelled)
        normalized.initialize()
        material = SQLiteCompanyMaterialIndex(sources_path, facts=writer,
            documents=documents, raw_files=raw_files, event_policy=event_policy, sqlite_policy=sqlite_policy,
            cancel_requested=cancelled)
        material.initialize()
        material.register_disclosures(items)
        measurement = _CompanySyncMeasurement()
        started = time.monotonic()
        extracted: list[tuple[Disclosure, CompanyDocumentExtraction]] = []
        failures: list[str] = []
        lock = Lock()
        with ExitStack() as resources:
            _observe_company_operations(resources, measurement)
            sessions = tuple(resources.enter_context(OpenExternalHttp()) for _ in range(count))
            engine = CompanyOcrEngine(ocr, timeout_seconds=configuration.document_ocr_timeout_seconds,
                page_timeout_seconds=configuration.document_ocr_page_timeout_seconds, max_pages=configuration.document_ocr_max_pages,
                dpi=configuration.document_ocr_dpi, cancel_requested=cancelled)
            pacer = CompanyRequestPacer(configuration.request_interval_seconds, time.monotonic, time.sleep, cancelled)
            def make_source(session: HttpTransport, source_pacer: CompanyRequestPacer,
                            engine: CompanyOcrEngine, raw_files: CompanyRawFiles) -> CninfoMarketDisclosureSource:
                return CninfoMarketDisclosureSource(session, monotonic=time.monotonic, sleeper=time.sleep,
                    timeout_seconds=configuration.source_timeout_seconds,
                    document_download_timeout_seconds=configuration.document_download_timeout_seconds,
                    document_parse_timeout_seconds=configuration.document_parse_timeout_seconds,
                    document_limits=configuration.document_limits, extended_document_limits=configuration.extended_document_limits,
                    request_interval_seconds=configuration.request_interval_seconds, cancel_requested=cancelled,
                    ocr_engine=engine, document_policies=policies, text_capability=text, raw_files=raw_files,
                    text_reserve_bytes=configuration.text_task_reserve_bytes, ocr_reserve_bytes=configuration.ocr_task_reserve_bytes,
                    pacer=source_pacer)
            sources = tuple(make_source(session, pacer, engine, raw_files) for session in sessions)
            workers = BoundedExecutor(worker_count=count + int(target is not None), queue_capacity=0,
                                      thread_name_prefix="company-document-research")
            workers.start()
            resources.callback(workers.stop, wait=True, cancel_futures=True)
            directory_future = None
            if target is not None:
                directory_pacer = CompanyRequestPacer(configuration.request_interval_seconds, time.monotonic, time.sleep, cancelled) if independent_hosts else pacer
                directory_source = make_source(resources.enter_context(OpenExternalHttp()), directory_pacer, engine, raw_files)
                directory_future = workers.submit(_probe_directory, directory_source, target)
                if directory_future is None:
                    raise RuntimeError("company_directory_probe_rejected")
            def extract(source: CninfoMarketDisclosureSource, rows: tuple[Disclosure, ...],
                        material: SQLiteCompanyMaterialIndex, lock: Lock,
                        extracted: list[tuple[Disclosure, CompanyDocumentExtraction]], failures: list[str]) -> None:
                for item in rows:
                    check()
                    try:
                        source.download_document(item)
                        try:
                            result = source.extract_document(item)
                        except CompanyDocumentFailure as error:
                            route = CompanyDocumentRouteForFailure(error.reason)
                            if route == "standard":
                                raise
                            result = source.extract_document(item, route=route)
                        material.commit_document(item, result, now)
                        with lock:
                            extracted.append((item, result))
                    except (OSError, RuntimeError, ValueError) as error:
                        with lock:
                            failures.append(_failure_category(error))
            futures = tuple(workers.submit(extract, source, items[offset::count], material, lock, extracted, failures)
                            for offset, source in enumerate(sources))
            for future in futures:
                if future is None:
                    raise RuntimeError("company_document_probe_rejected")
                future.result()
            directory_result = directory_future.result() if directory_future is not None else None
            check()
            codes = tuple(sorted({code for item in items for code in item.codes}))
            typed_documents = tuple(CompanyDocument(item, extraction.text, True) for item, extraction in sorted(extracted, key=lambda pair: (pair[0].source, pair[0].identity)))
            facts = tuple(CompileCompanyFacts(CompanyEvidenceInput(code, now, now.date()-timedelta(days=365), now.date(),
                writer.extraction_hash.hex(), tuple(item for item in typed_documents if code in item.disclosure.codes),
                False, (), None, (), False, (), False, ()), event_policy) for code in codes)
            receipts = tuple(CompanyInputReceipt(item.source, item.identity, code, item.digest,
                writer.extraction_hash, hashlib.sha256(extraction.text.encode()).digest(), item.published_at)
                for item, extraction in extracted for code in item.codes)
            fields = tuple(CompanyFieldConclusion(item.code, item.event_id, cast(CompanyFieldName, item.field),
                float(item.value) if type(item.value) is int else item.value, cast(CompanyFieldConclusionState, item.state),
                item.applicable, item.reason, ()) for fact in facts for item in BuildCompanyFieldResults(fact))
            identity = CompanyPublicationIdentity(bytes.fromhex(CanonicalDigest((facts, now))), writer.extraction_hash, now)
            baseline = CompanyNormalizationBaseline(facts, fields, receipts,
                tuple(sorted((item.identity, item.source) for item in items)), identity, CompanyFieldProofRulesHash())
            normalized.seed(baseline)
            prepared_fields: list[CompanyFieldConclusion] = []
            prepared_receipts: list[CompanyInputReceipt] = []
            with SqliteSession(material.path, SqliteConnectionOptions(read_only=True), sqlite_policy=sqlite_policy) as raw, SqliteSession(
                writer.path, SqliteConnectionOptions(read_only=True), sqlite_policy=sqlite_policy,
            ) as formal, SqliteTransaction(raw), SqliteTransaction(formal):
                for fact in facts:
                    prepared = PrepareCompanyFieldResults(raw, formal, fact, (), observed_at=now,
                        extraction_hash=writer.extraction_hash, event_policy=event_policy, read_document=documents.read)
                    prepared_fields.extend(prepared.results)
                    prepared_receipts.extend(prepared.receipts)
            normalized.publish_fields(codes, tuple(prepared_fields), tuple(prepared_receipts), now,
                                      field_rules_hash=CompanyFieldProofRulesHash())
            elapsed = time.monotonic()-started
            reader = SQLiteCompanyEvidenceReader(writer.path, event_policy=event_policy, sqlite_policy=sqlite_policy)
            actual = reader.read(codes)
            before = reader.publication_identity()
            normalized.seed(baseline)
            resumed = normalized.publish_fields(codes, tuple(prepared_fields), tuple(prepared_receipts), now,
                                                field_rules_hash=CompanyFieldProofRulesHash())
            restart_equal = not resumed and reader.publication_identity() == before
        signatures = tuple((item.source, item.identity, extraction.payload_hash.hex(), hashlib.sha256(extraction.text.encode()).hexdigest()) for item, extraction in sorted(extracted, key=lambda pair: (pair[0].source, pair[0].identity)))
        runs.append({"workers": count, "pacing": "independent_hosts" if independent_hosts else "shared",
            "seconds": elapsed, "sample_documents": len(items), "extracted_documents": len(extracted), "failures": failures,
            "directory": directory_result, "facts_digest": CanonicalDigest(actual), "documents_digest": CanonicalDigest(signatures),
            "restart_unchanged": restart_equal, "operations": measurement.report()})
    same_documents = len(runs) == 2 and runs[0]["documents_digest"] == runs[1]["documents_digest"]
    same_facts = len(runs) == 2 and runs[0]["facts_digest"] == runs[1]["facts_digest"]
    same_directory = not compare_pacing or runs[0]["directory"] == runs[1]["directory"] and isinstance(runs[0]["directory"], dict) and runs[0]["directory"].get("complete") is True
    passed = same_documents and same_facts and same_directory and all(
        run["extracted_documents"] == len(items) and not run["failures"] and run["restart_unchanged"] for run in runs)
    return { "status": "passed" if passed else "degraded",
        "summary": {"pipeline_audit": {"isolated_sample": True, "source_admission_verified": False,
            "production_enabled": False, "performance_accepted": False, "full_sync_verified": False,
            "equivalent_documents": same_documents, "equivalent_facts": same_facts, "runs": runs}},
        "findings": [] if passed else ["company_pipeline_probe_incomplete"]}
