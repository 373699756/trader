"""Bounded process supervision for the explicit BaoStock research download."""

from __future__ import annotations

import errno
import hashlib
import importlib.metadata
import os
import shutil
import sqlite3
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from multiprocessing import get_context
from multiprocessing.connection import Connection
from multiprocessing.context import SpawnContext
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import IO, Literal, Protocol, cast

from trader.application.research.baostock_daily import BaoStockShardContext
from trader.application.research.baostock_history_runtime import (
    BaoStockRuntimePhase,
    BaoStockRuntimeProgress,
    BaoStockRuntimeProgressPort,
    BaoStockRuntimeRequest,
    BaoStockRuntimeState,
    BaoStockRuntimeStatus,
)
from trader.domain.research.baostock_daily import (
    BaoStockDailyManifest,
    BaoStockDailySpec,
    BaoStockSecurity,
    BaoStockV3DatasetManifest,
    build_baostock_v3_dataset_manifest,
)
from trader.domain.research.historical_effective_facts import (
    HistoricalEffectiveFactsAudit,
    HistoricalEffectiveFactsProbe,
    build_historical_effective_facts_audit,
)
from trader.infra.research.baostock_daily import (
    BaoStockDailyArtifactConflictError,
    BaoStockDailyPartitionedArchive,
    BaoStockRowGateway,
    BaoStockRowResult,
    BaoStockSdkPort,
    BaoStockShardSnapshot,
    SQLiteBaoStockDailyShard,
)
from trader.infra.research.baostock_v3_dataset import (
    BaoStockV3DatasetArtifactConflictError,
    BaoStockV3DatasetArtifactStore,
)
from trader.infra.research.historical_effective_facts import (
    HistoricalEffectiveFactsArtifactConflictError,
    HistoricalEffectiveFactsArtifactStore,
)

BAOSTOCK_CANCEL_GRACE_SECONDS = 10.0
BAOSTOCK_QUERY_INTERVAL_SECONDS = 2.0
BAOSTOCK_MAX_RSS_MB = 4096.0
BAOSTOCK_MIN_AVAILABLE_DISK_GIB = 25.0
BAOSTOCK_LOW_DISK_WATERMARK_GIB = 2.0


class _BaoStockSessionSdkPort(BaoStockSdkPort, Protocol):
    def login(self) -> BaoStockRowResult: ...

    def logout(self) -> BaoStockRowResult: ...


@dataclass(frozen=True)
class _ContextResponse:
    context: BaoStockShardContext | None
    failure_reason: str = ""


@dataclass(frozen=True)
class _ContextStage:
    phase: BaoStockRuntimePhase


@dataclass(frozen=True)
class _SupplierCallActivity:
    state: Literal["started", "completed"]


@dataclass(frozen=True)
class _WorkerReady:
    failure_reason: str = ""


@dataclass(frozen=True)
class _DownloadCommand:
    security: BaoStockSecurity


@dataclass(frozen=True)
class _DownloadResponse:
    code: str
    succeeded: bool
    failure_reason: str = ""


@dataclass(frozen=True)
class _StopCommand:
    pass


@dataclass
class _WorkerHandle:
    process: BaseProcess
    connection: Connection
    shard_path: Path
    current: BaoStockSecurity | None = None
    started_at: float = 0.0


class _RateLimitedBaoStockSdk:
    """Keep every SDK query on one socket and at most one call per second."""

    def __init__(
        self,
        sdk: _BaoStockSessionSdkPort,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        activity: Callable[[Literal["started", "completed"]], None] | None = None,
    ) -> None:
        self.__version__ = sdk.__version__
        self._sdk = sdk
        self._monotonic = monotonic
        self._sleep = sleep
        self._activity = activity or (lambda _state: None)
        self._last_query_started: float | None = None

    def query_trade_dates(self, *, start_date: str, end_date: str) -> BaoStockRowResult:
        return self._query(lambda: self._sdk.query_trade_dates(start_date=start_date, end_date=end_date))

    def query_stock_basic(self) -> BaoStockRowResult:
        return self._query(self._sdk.query_stock_basic)

    def query_stock_industry(self, *, code: str = "", date: str = "") -> BaoStockRowResult:
        return self._query(lambda: self._sdk.query_stock_industry(code=code, date=date))

    def query_history_k_data_plus(  # noqa: PLR0913 - exact third-party SDK signature
        self,
        code: str,
        fields: str,
        start_date: str,
        end_date: str,
        *,
        frequency: str,
        adjustflag: str,
    ) -> BaoStockRowResult:
        return self._query(
            lambda: self._sdk.query_history_k_data_plus(
                code,
                fields,
                start_date,
                end_date,
                frequency=frequency,
                adjustflag=adjustflag,
            )
        )

    def _query(self, call: Callable[[], BaoStockRowResult]) -> BaoStockRowResult:
        self._wait()
        self._activity("started")
        try:
            return call()
        finally:
            self._activity("completed")

    def _wait(self) -> None:
        now = self._monotonic()
        if self._last_query_started is not None:
            remaining = BAOSTOCK_QUERY_INTERVAL_SECONDS - (now - self._last_query_started)
            if remaining > 0:
                self._sleep(remaining)
                now = self._monotonic()
        self._last_query_started = now


def run_baostock_history(
    request: BaoStockRuntimeRequest,
    repository_root: Path,
    *,
    cancel_requested: Callable[[], bool] | None = None,
    progress: BaoStockRuntimeProgressPort | None = None,
) -> BaoStockRuntimeStatus:
    request.validate(repository_root)
    _emit_progress(progress, BaoStockRuntimeProgress("preflight", sessions=request.sessions))
    if request.sessions == 2000 and _available_disk_gb(request.runtime_dir) < BAOSTOCK_MIN_AVAILABLE_DISK_GIB:
        return BaoStockRuntimeStatus(
            state="resource_blocked",
            sessions=request.sessions,
            failure_reasons=("disk_below_25gb",),
        )
    cancel = cancel_requested or (lambda: False)
    root = _runtime_root(request.runtime_dir, request.sessions)
    try:
        with _DownloadLock(root / ".download.lock"):
            return _run_locked(request, root, cancel, progress)
    except BlockingIOError:
        return BaoStockRuntimeStatus(
            state="locked",
            sessions=request.sessions,
            failure_reasons=("download_locked",),
        )
    except (
        BaoStockDailyArtifactConflictError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
        sqlite3.DatabaseError,
    ) as exc:
        return BaoStockRuntimeStatus(
            state="failed",
            sessions=request.sessions,
            failure_reasons=(_failure_code(exc),),
        )


def inspect_baostock_history(runtime_dir: Path, *, sessions: int = 2000) -> BaoStockRuntimeStatus:
    root = _runtime_root(runtime_dir, sessions)
    if not (root / "manifest.json").is_file():
        return BaoStockRuntimeStatus()
    try:
        store = BaoStockDailyPartitionedArchive(root)
        value = store.verify()
        descriptor = store.describe_frozen_daily_input()
        facts = HistoricalEffectiveFactsArtifactStore(root).verify()
        dataset = BaoStockV3DatasetArtifactStore(root).verify()
    except (
        BaoStockDailyArtifactConflictError,
        BaoStockV3DatasetArtifactConflictError,
        HistoricalEffectiveFactsArtifactConflictError,
    ):
        return BaoStockRuntimeStatus(state="failed", failure_reasons=("manifest_invalid",))
    audit = value.audit
    return BaoStockRuntimeStatus(
        state="completed" if audit.status == "coverage_ready" else "completed_with_failures",
        sessions=descriptor.requested_sessions,
        shard_count=len(tuple((root / "shards").glob("*.sqlite3"))),
        universe_count=audit.universe_count,
        completed_codes=len(audit.code_coverages),
        failed_codes=len(audit.failed_codes),
        manifest_hash=value.content_hash,
        coverage_status=audit.status,
        historical_effective_facts_status=facts.status,
        historical_effective_facts_hash=facts.content_hash,
        v3_dataset_status=dataset.status,
        v3_dataset_hash=dataset.content_hash,
        failure_reasons=audit.failure_reasons,
    )


def project_baostock_runtime_status(status: BaoStockRuntimeStatus) -> dict[str, object]:
    return {
        "schema_version": status.schema_version,
        "state": status.state,
        "sessions": status.sessions,
        "shard_count": status.shard_count,
        "universe_count": status.universe_count,
        "completed_codes": status.completed_codes,
        "failed_codes": status.failed_codes,
        "peak_rss_mb": status.peak_rss_mb,
        "manifest_hash": status.manifest_hash,
        "coverage_status": status.coverage_status,
        "historical_effective_facts_status": status.historical_effective_facts_status,
        "historical_effective_facts_hash": status.historical_effective_facts_hash,
        "v3_dataset_status": status.v3_dataset_status,
        "v3_dataset_hash": status.v3_dataset_hash,
        "failure_reasons": list(status.failure_reasons),
        "production_authority": status.production_authority,
        "point_in_time_parity": status.point_in_time_parity,
    }


def _run_locked(
    request: BaoStockRuntimeRequest,
    root: Path,
    cancel_requested: Callable[[], bool],
    progress: BaoStockRuntimeProgressPort | None,
) -> BaoStockRuntimeStatus:
    _quarantine_corrupt_archive_parts(root)
    if (root / "manifest.json").is_file():
        store = BaoStockDailyPartitionedArchive(root)
        manifest = store.verify()
        descriptor = store.describe_frozen_daily_input()
        if descriptor.requested_sessions != request.sessions:
            raise BaoStockDailyArtifactConflictError("BaoStock completed manifest uses different sessions")
        _seal_research_handoff(root, manifest)
        return inspect_baostock_history(request.runtime_dir, sessions=request.sessions)
    root.mkdir(parents=True, exist_ok=True)
    spec = BaoStockDailySpec(sessions=request.sessions)
    process_context = get_context("spawn")
    _emit_progress(progress, BaoStockRuntimeProgress("checkpoint_loading", sessions=request.sessions))
    context = _load_resume_context(root, spec)
    context_failure = ""
    if context is None:
        context, context_failure = _fetch_context(process_context, spec, request, progress)
    if context is None:
        state: BaoStockRuntimeState = (
            "dependency_unavailable" if context_failure == "dependency_unavailable" else "failed"
        )
        return BaoStockRuntimeStatus(
            state=state,
            sessions=request.sessions,
            failure_reasons=(context_failure or "supplier_context_failed",),
        )
    universe = tuple(item for item in context.universe if context.calendar.expected_dates(item))
    if not universe:
        return BaoStockRuntimeStatus(
            state="failed",
            sessions=request.sessions,
            failure_reasons=("security_window_empty",),
        )
    bounded_context = BaoStockShardContext(
        context.calendar, universe, context.source_versions, context.industry_intervals
    )
    _migrate_legacy_archive(root, spec, context)
    expected_records = sum(len(context.calendar.expected_dates(item)) for item in universe)
    _emit_progress(
        progress,
        BaoStockRuntimeProgress(
            "database_initializing",
            sessions=request.sessions,
            universe_count=len(universe),
            expected_records=expected_records,
        ),
    )
    return _DownloadCoordinator(
        _DownloadRun(process_context, request, spec, bounded_context, root, cancel_requested, progress)
    ).run()


def _quarantine_corrupt_archive_parts(root: Path) -> None:
    """Remove only invalid immutable metadata/shards so healthy partitions remain resumable."""
    manifest_path = root / "manifest.json"
    catalog_path = root / "catalog.sqlite3"
    if not manifest_path.exists():
        return
    corrupt = _corrupt_partition_paths(root, manifest_path, catalog_path)
    if not corrupt and catalog_path.exists():
        return
    quarantine = root / "quarantine" / f"recovery-{int(time.time())}"
    quarantine.mkdir(parents=True, exist_ok=True)
    for path in (*corrupt, manifest_path, catalog_path):
        if path.exists():
            shutil.move(str(path), quarantine / path.name)


def _corrupt_partition_paths(root: Path, manifest_path: Path, catalog_path: Path) -> tuple[Path, ...]:
    corrupt: list[Path] = []
    try:
        import json

        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        partitions = payload.get("partitions")
        if not isinstance(partitions, list):
            raise ValueError("partitions missing")
        expected_catalog = payload.get("catalog_sha256")
        if (
            not isinstance(expected_catalog, str)
            or not catalog_path.exists()
            or _sha256_file(catalog_path) != expected_catalog
        ):
            raise ValueError("catalog hash mismatch")
        for item in partitions:
            if not isinstance(item, dict):
                raise ValueError("partition descriptor invalid")
            relative = item.get("relative_path")
            expected = item.get("database_sha256")
            if not isinstance(relative, str) or not isinstance(expected, str):
                raise ValueError("partition identity invalid")
            path = root / relative
            digest = _sha256_file(path)
            if digest != expected:
                corrupt.append(path)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return ()
    return tuple(corrupt)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_resume_context(root: Path, spec: BaoStockDailySpec) -> BaoStockShardContext | None:
    contexts: list[BaoStockShardContext] = []
    for path in sorted((root / "shards").glob("*.sqlite3")):
        context = SQLiteBaoStockDailyShard(path).context(spec)
        if context is not None:
            contexts.append(context)
    if not contexts:
        return None
    first = contexts[0]
    if any(context != first for context in contexts[1:]):
        raise BaoStockDailyArtifactConflictError("BaoStock resume shard contexts do not match")
    return first


def _migrate_legacy_archive(root: Path, spec: BaoStockDailySpec, context: BaoStockShardContext) -> None:
    """Move the pre-partition archive only after taking its real lock and copying rows."""
    repository_root = root.parents[3] if len(root.parents) > 3 else root.parent
    legacy = repository_root / "trader" / "data" / "history" / "baostock-daily" / root.name
    if legacy == root or not legacy.is_dir() or not tuple(legacy.glob("shard-*.sqlite3")):
        return
    recovery = root / "recovery" / f"legacy-{int(time.time())}"
    with _DownloadLock(legacy / ".download.lock"):
        legacy_shards = tuple(sorted(legacy.glob("shard-*.sqlite3")))
        for path in legacy_shards:
            snapshot = SQLiteBaoStockDailyShard(path).snapshot(spec)
            for batch in snapshot.batches:
                target = SQLiteBaoStockDailyShard(
                    root
                    / "shards"
                    / _partition_name(
                        next(item.board for item in context.universe if item.code == batch.code), batch.code
                    )
                )
                target.initialize(
                    spec,
                    context.calendar,
                    context.universe,
                    context.source_versions,
                    context.industry_intervals,
                )
                target.save_batch(spec, batch)
        recovery.mkdir(parents=True, exist_ok=True)
        for path in legacy.iterdir():
            if path.name == ".download.lock":
                continue
            shutil.move(str(path), recovery / path.name)


def _fetch_context(
    process_context: SpawnContext,
    spec: BaoStockDailySpec,
    request: BaoStockRuntimeRequest,
    progress: BaoStockRuntimeProgressPort | None,
) -> tuple[BaoStockShardContext | None, str]:
    last_failure = "supplier_context_failed"
    for _ in range(request.retries + 1):
        parent, child = process_context.Pipe()
        process = process_context.Process(target=_context_worker_main, args=(child, spec))
        process.start()
        child.close()
        try:
            deadline = time.monotonic() + request.timeout_seconds
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not parent.poll(remaining):
                    last_failure = "supplier_call_timeout"
                    _terminate_process(process)
                    break
                response = parent.recv()
                if isinstance(response, _SupplierCallActivity):
                    deadline = time.monotonic() + request.timeout_seconds
                    continue
                if isinstance(response, _ContextStage):
                    _emit_progress(progress, BaoStockRuntimeProgress(response.phase, sessions=request.sessions))
                    deadline = time.monotonic() + request.timeout_seconds
                    continue
                if not isinstance(response, _ContextResponse):
                    last_failure = "supplier_context_protocol_invalid"
                    break
                if response.context is not None:
                    process.join(timeout=1.0)
                    return response.context, ""
                last_failure = response.failure_reason or "supplier_context_failed"
                break
            if last_failure in {
                "dependency_unavailable",
                "supplier_login_failed_blacklisted",
                "supplier_query_failed_blacklisted",
            }:
                break
        except (EOFError, OSError):
            last_failure = "supplier_context_process_failed"
        finally:
            parent.close()
            _terminate_process(process)
    return None, last_failure


def _context_worker_main(connection: Connection, spec: BaoStockDailySpec) -> None:
    _silence_vendor_output()
    sdk: _BaoStockSessionSdkPort | None = None
    try:
        connection.send(_ContextStage("supplier_login"))
        sdk = _load_sdk()
        _login(sdk)
        gateway = BaoStockRowGateway(
            _RateLimitedBaoStockSdk(
                sdk,
                activity=lambda state: connection.send(_SupplierCallActivity(state)),
            ),
            dependency_versions=_dependency_versions(),
        )
        connection.send(_ContextStage("trading_calendar"))
        calendar = gateway.fetch_calendar(spec)
        connection.send(_ContextStage("security_universe"))
        universe = gateway.fetch_universe(spec)
        intervals = gateway.fetch_industry_intervals(spec, calendar, universe)
        connection.send(
            _ContextResponse(
                BaoStockShardContext(
                    calendar,
                    universe,
                    gateway.source_versions(),
                    intervals,
                )
            )
        )
    except Exception as exc:
        connection.send(_ContextResponse(None, _failure_code(exc)))
    finally:
        if sdk is not None:
            _logout(sdk)
        connection.close()


@dataclass(frozen=True)
class _DownloadRun:
    process_context: SpawnContext
    request: BaoStockRuntimeRequest
    spec: BaoStockDailySpec
    context: BaoStockShardContext
    root: Path
    cancel_requested: Callable[[], bool]
    progress: BaoStockRuntimeProgressPort | None


class _DownloadCoordinator:
    def __init__(self, run: _DownloadRun) -> None:
        self._run = run
        self._shards = tuple(
            SQLiteBaoStockDailyShard(run.root / "shards" / _partition_name(item.board, item.code))
            for item in run.context.universe
            if item.code == _partition_first_code(run.context.universe, item.board, item.code)
        )
        self._shards_by_name = {shard.path.name: shard for shard in self._shards}
        self._pending: deque[BaoStockSecurity] = deque()
        self._attempts: dict[str, int] = {}
        self._terminal_failures: dict[str, str] = {}
        self._handles: list[_WorkerHandle] = []
        self._peak_rss_mb = 0.0
        self._cancelling_since: float | None = None
        self._resource_blocked = False
        self._resource_block_reason = ""
        self._run_failure_reason = ""
        self._completed_codes: set[str] = set()
        self._ready_codes: set[str] = set()
        self._failed_codes: set[str] = set()
        self._downloaded_records = 0
        self._expected_records = sum(len(run.context.calendar.expected_dates(item)) for item in run.context.universe)

    def run(self) -> BaoStockRuntimeStatus:
        self._initialize_shards()
        self._report("worker_starting")
        startup_failure = self._start_workers()
        if not self._handles:
            return self._partial(
                "failed",
                (startup_failure or "worker_start_failed",),
                phase="worker_starting",
            )
        self._report("downloading")
        try:
            self._drive_workers()
        finally:
            for handle in self._handles:
                _stop_worker(
                    handle,
                    graceful=not self._resource_blocked and handle.current is None,
                )
        return self._finish()

    def _initialize_shards(self) -> None:
        run = self._run
        for shard in self._shards:
            shard.initialize(
                run.spec,
                run.context.calendar,
                run.context.universe,
                run.context.source_versions,
                run.context.industry_intervals,
            )
        self._refresh_checkpoint_progress()
        self._pending.extend(item for item in run.context.universe if item.code not in self._ready_codes)

    def _refresh_checkpoint_progress(self) -> tuple[BaoStockShardSnapshot, ...]:
        snapshots = tuple(shard.snapshot(self._run.spec) for shard in self._shards)
        self._completed_codes = {batch.code for snapshot in snapshots for batch in snapshot.batches}
        self._ready_codes = {code for shard in self._shards for code in shard.training_ready_codes(self._run.spec)}
        self._failed_codes = {code for snapshot in snapshots for code, _ in snapshot.failures}
        self._downloaded_records = sum(len(batch.cells) for snapshot in snapshots for batch in snapshot.batches)
        return snapshots

    def _start_workers(self) -> str:
        failure = ""
        for _ in range(self._run.request.workers):
            handle, failure = _start_worker(
                self._run.process_context,
                self._run.spec,
                self._run.context,
                self._run.root,
                self._run.request,
            )
            if handle is not None:
                self._handles.append(handle)
        self._peak_rss_mb = _process_group_rss_mb(self._handles)
        return failure

    def _drive_workers(self) -> None:
        while self._pending or self._busy():
            now = time.monotonic()
            if self._cancelling_since is None and self._run.cancel_requested():
                self._cancelling_since = now
            for handle in self._handles:
                self._service_worker(handle, now)
                if self._run_failure_reason:
                    return
            self._peak_rss_mb = max(self._peak_rss_mb, _process_group_rss_mb(self._handles))
            if self._peak_rss_mb > BAOSTOCK_MAX_RSS_MB:
                self._resource_blocked = True
                self._resource_block_reason = "rss_above_4gb"
                return
            if self._cancellation_finished(now):
                return
            if self._cancelling_since is None:
                self._assign_pending()
                if self._pending and not any(handle.process.is_alive() for handle in self._handles):
                    self._record_unavailable()
                    return
            time.sleep(0.02)

    def _service_worker(self, handle: _WorkerHandle, now: float) -> None:
        if handle.current is None:
            if not handle.process.is_alive() and self._cancelling_since is None:
                self._replace(handle)
            return
        if handle.connection.poll():
            self._accept_response(handle, now)
            return
        if not handle.process.is_alive():
            security = self._release(handle)
            self._retry_or_record(security, "worker_process_failed")
            self._replace(handle)
            return
        if now - handle.started_at > self._run.request.timeout_seconds:
            security = self._release(handle)
            _terminate_process(handle.process)
            self._retry_or_record(security, "supplier_call_timeout")
            if not self._replace(handle):
                self._terminal_failures[security.code] = "worker_restart_failed"

    def _accept_response(self, handle: _WorkerHandle, now: float) -> None:
        failure_reason = ""
        try:
            response: object | None = handle.connection.recv()
        except (EOFError, OSError):
            response = None
        if isinstance(response, _SupplierCallActivity):
            handle.started_at = now
            return
        security = self._release(handle)
        if response is None:
            response = _DownloadResponse(security.code, False, "worker_process_failed")
        if not isinstance(response, _DownloadResponse) or response.code != security.code:
            failure_reason = "worker_protocol_invalid"
            self._retry_or_record(security, failure_reason)
        elif not response.succeeded:
            failure_reason = response.failure_reason or "supplier_query_failed"
            self._retry_or_record(security, failure_reason)
        else:
            self._attempts.pop(security.code, None)
            self._terminal_failures.pop(security.code, None)
            self._failed_codes.discard(security.code)
            self._completed_codes.add(security.code)
            self._ready_codes.add(security.code)
            self._downloaded_records += len(self._run.context.calendar.expected_dates(security))
            for shard in self._shards:
                shard.clear_failure(self._run.spec, security.code)
            if _available_disk_gb(self._run.request.runtime_dir) < BAOSTOCK_LOW_DISK_WATERMARK_GIB:
                self._resource_blocked = True
                self._resource_block_reason = "disk_low_watermark"
        self._report("downloading", failure_reason)

    def _retry_or_record(self, security: BaoStockSecurity, reason: str) -> None:
        if reason == "supplier_query_failed_blacklisted":
            self._record_terminal_failure(security, reason)
            self._run_failure_reason = reason
            return
        failures = self._attempts.get(security.code, 0) + 1
        self._attempts[security.code] = failures
        if failures <= self._run.request.retries:
            self._pending.appendleft(security)
            return
        bounded_reason = reason if _valid_failure_code(reason) else "supplier_query_failed"
        self._record_terminal_failure(security, bounded_reason)

    def _record_terminal_failure(self, security: BaoStockSecurity, reason: str) -> None:
        self._terminal_failures[security.code] = reason
        self._failed_codes.add(security.code)
        self._failure_shard(security).record_failure(self._run.spec, security.code, reason)

    def _assign_pending(self) -> None:
        for handle in self._handles:
            if handle.current is None and handle.process.is_alive() and self._pending:
                security = self._pending.popleft()
                handle.connection.send(_DownloadCommand(security))
                handle.current = security
                handle.started_at = time.monotonic()

    def _record_unavailable(self) -> None:
        while self._pending:
            security = self._pending.popleft()
            self._terminal_failures[security.code] = "worker_unavailable"
            self._failed_codes.add(security.code)
            self._failure_shard(security).record_failure(self._run.spec, security.code, "worker_unavailable")

    def _replace(self, handle: _WorkerHandle) -> bool:
        return _replace_worker(
            handle,
            self._run.process_context,
            self._run.spec,
            self._run.context,
            self._run.request,
        )

    def _release(self, handle: _WorkerHandle) -> BaoStockSecurity:
        security = handle.current
        if security is None:
            raise RuntimeError("worker has no active security")
        handle.current = None
        return security

    def _busy(self) -> bool:
        return any(handle.current is not None for handle in self._handles)

    def _cancellation_finished(self, now: float) -> bool:
        if self._cancelling_since is None:
            return False
        return not self._busy() or now - self._cancelling_since >= BAOSTOCK_CANCEL_GRACE_SECONDS

    def _failure_shard(self, security: BaoStockSecurity) -> SQLiteBaoStockDailyShard:
        return self._shards_by_name[_partition_name(security.board, security.code)]

    def _finish(self) -> BaoStockRuntimeStatus:
        if self._run_failure_reason:
            return self._partial("failed", (self._run_failure_reason,))
        if self._resource_blocked:
            return self._partial("resource_blocked", (self._resource_block_reason or "rss_above_4gb",))
        if self._cancelling_since is not None:
            return self._partial("cancelled", ("cancelled",))
        if self._terminal_failures:
            return self._partial("completed_with_failures", tuple(self._terminal_failures.values()))
        snapshots = self._refresh_checkpoint_progress()
        completed = frozenset(code for shard in self._shards for code in shard.training_ready_codes(self._run.spec))
        if completed != frozenset(item.code for item in self._run.context.universe):
            return self._partial("completed_with_failures", ("incomplete_codes",))
        return self._publish(snapshots, completed)

    def _publish(
        self,
        snapshots: tuple[BaoStockShardSnapshot, ...],
        completed: frozenset[str],
    ) -> BaoStockRuntimeStatus:
        self._report("merging")
        partitioned = BaoStockDailyPartitionedArchive(self._run.root).write(
            self._run.spec,
            tuple(self._shards),
        )
        facts, dataset = _seal_research_handoff(self._run.root, partitioned)
        audit = partitioned.audit
        return BaoStockRuntimeStatus(
            state="completed" if audit.status == "coverage_ready" else "completed_with_failures",
            sessions=self._run.request.sessions,
            shard_count=len(partitioned.partitions),
            universe_count=len(self._run.context.universe),
            completed_codes=len(completed),
            failed_codes=len(audit.failed_codes),
            peak_rss_mb=self._peak_rss_mb,
            manifest_hash=partitioned.content_hash,
            coverage_status=audit.status,
            historical_effective_facts_status=facts.status,
            historical_effective_facts_hash=facts.content_hash,
            v3_dataset_status=dataset.status,
            v3_dataset_hash=dataset.content_hash,
            failure_reasons=audit.failure_reasons,
        )

    def _partial(
        self,
        state: BaoStockRuntimeState,
        reasons: tuple[str, ...],
        *,
        phase: BaoStockRuntimePhase = "downloading",
    ) -> BaoStockRuntimeStatus:
        self._refresh_checkpoint_progress()
        failure_reasons = tuple(reasons or ("incomplete_codes",))
        self._report(phase, failure_reasons[0])
        return BaoStockRuntimeStatus(
            state=state,
            sessions=self._run.request.sessions,
            shard_count=len(self._shards),
            universe_count=len(self._run.context.universe),
            completed_codes=len(self._completed_codes),
            failed_codes=len(self._failed_codes),
            peak_rss_mb=self._peak_rss_mb,
            failure_reasons=failure_reasons,
        )

    def _report(self, phase: BaoStockRuntimePhase, last_failure_reason: str = "") -> None:
        current_code = ""
        for handle in self._handles:
            security = handle.current
            if security is not None:
                current_code = security.code
                break
        _emit_progress(
            self._run.progress,
            BaoStockRuntimeProgress(
                phase,
                sessions=self._run.request.sessions,
                universe_count=len(self._run.context.universe),
                completed_codes=len(self._completed_codes),
                failed_codes=len(self._failed_codes),
                expected_records=self._expected_records,
                downloaded_records=self._downloaded_records,
                active_workers=sum(handle.process.is_alive() for handle in self._handles),
                current_code=current_code,
                # The worker owns the two-second query pacing. It does not expose
                # a pending sleep, so a zero value truthfully means no known hold.
                rate_limit_cooldown_seconds=0.0,
                last_failure_reason=(
                    last_failure_reason if _valid_failure_code(last_failure_reason) else "supplier_query_failed"
                )
                if last_failure_reason
                else "",
            ),
        )


def _seal_research_handoff(
    root: Path,
    daily: BaoStockDailyManifest,
) -> tuple[HistoricalEffectiveFactsAudit, BaoStockV3DatasetManifest]:
    facts = HistoricalEffectiveFactsArtifactStore(root).write(
        build_historical_effective_facts_audit(
            (
                HistoricalEffectiveFactsProbe(
                    "baostock_daily_training",
                    daily.audit.calendar_first_date,
                    True,
                    True,
                    True,
                    True,
                ),
            )
        )
    )
    dataset = BaoStockV3DatasetArtifactStore(root).write(
        build_baostock_v3_dataset_manifest(
            daily,
            facts,
            BaoStockDailyPartitionedArchive(root).complete_dates(),
        )
    )
    return facts, dataset


def _start_worker(
    process_context: SpawnContext,
    spec: BaoStockDailySpec,
    context: BaoStockShardContext,
    shard_path: Path,
    request: BaoStockRuntimeRequest,
) -> tuple[_WorkerHandle | None, str]:
    last_failure = "worker_start_failed"
    for _ in range(request.retries + 1):
        parent, child = process_context.Pipe()
        process = process_context.Process(target=_download_worker_main, args=(child, spec, context, str(shard_path)))
        process.start()
        child.close()
        handle = _WorkerHandle(process, parent, shard_path)
        try:
            if not parent.poll(request.timeout_seconds):
                last_failure = "supplier_call_timeout"
            else:
                response = parent.recv()
                if isinstance(response, _WorkerReady) and not response.failure_reason:
                    return handle, ""
                last_failure = (
                    response.failure_reason if isinstance(response, _WorkerReady) else "worker_protocol_invalid"
                )
                if last_failure in {"dependency_unavailable", "supplier_login_failed_blacklisted"}:
                    break
        except (EOFError, OSError):
            last_failure = "worker_process_failed"
        parent.close()
        _terminate_process(process)
    return None, last_failure


def _replace_worker(
    handle: _WorkerHandle,
    process_context: SpawnContext,
    spec: BaoStockDailySpec,
    context: BaoStockShardContext,
    request: BaoStockRuntimeRequest,
) -> bool:
    replacement, _ = _start_worker(process_context, spec, context, handle.shard_path, request)
    if replacement is None:
        return False
    handle.connection.close()
    handle.process = replacement.process
    handle.connection = replacement.connection
    handle.current = None
    handle.started_at = 0.0
    return True


def _download_worker_main(
    connection: Connection,
    spec: BaoStockDailySpec,
    context: BaoStockShardContext,
    shard_path: str,
) -> None:
    _silence_vendor_output()
    sdk: _BaoStockSessionSdkPort | None = None
    try:
        archive_root = Path(shard_path)
        sdk = _load_sdk()
        _login(sdk)
        gateway = BaoStockRowGateway(
            _RateLimitedBaoStockSdk(
                sdk,
                activity=lambda state: connection.send(_SupplierCallActivity(state)),
            ),
            dependency_versions=context.source_versions.dependency_versions,
        )
        connection.send(_WorkerReady())
        while True:
            command = connection.recv()
            if isinstance(command, _StopCommand):
                break
            if not isinstance(command, _DownloadCommand):
                connection.send(_DownloadResponse("", False, "worker_protocol_invalid"))
                continue
            try:
                archive = SQLiteBaoStockDailyShard(
                    archive_root / "shards" / _partition_name(command.security.board, command.security.code)
                )
                archive.initialize(
                    spec,
                    context.calendar,
                    context.universe,
                    context.source_versions,
                    context.industry_intervals,
                )
                download = gateway.fetch_code_download(spec, command.security, context.calendar)
                archive.save_batch(spec, download.batch)
                intervals = tuple(item for item in context.industry_intervals if item.code == command.security.code)
                archive.save_training_facts(spec, command.security.code, download.daily_facts, intervals)
            except Exception as exc:
                connection.send(_DownloadResponse(command.security.code, False, _failure_code(exc)))
            else:
                connection.send(_DownloadResponse(command.security.code, True))
    except Exception as exc:
        connection.send(_WorkerReady(_failure_code(exc)))
    finally:
        if sdk is not None:
            _logout(sdk)
        connection.close()


def _shard_index(code: str, shard_count: int) -> int:
    return int(code) % shard_count


def _partition_name(board: str, code: str) -> str:
    if board not in {"main", "chinext", "star"} or len(code) != 6 or not code.isdigit():
        raise ValueError("BaoStock partition identity is invalid")
    # Keep the original first bucket name, then bound every partition to 100 codes.
    bucket = int(code[4:]) // 100
    suffix = "" if bucket == 0 else f"-{bucket:02d}"
    return f"{board}-{code[:4]}{suffix}.sqlite3"


def _partition_first_code(universe: tuple[BaoStockSecurity, ...], board: str, code: str) -> str:
    name = _partition_name(board, code)
    return min(item.code for item in universe if _partition_name(item.board, item.code) == name)


def _stop_worker(handle: _WorkerHandle, *, graceful: bool) -> None:
    if graceful and handle.process.is_alive():
        try:
            handle.connection.send(_StopCommand())
            handle.process.join(timeout=1.0)
        except (BrokenPipeError, OSError):
            pass
    handle.connection.close()
    _terminate_process(handle.process)


def _terminate_process(process: BaseProcess) -> None:
    if process.is_alive():
        process.terminate()
    process.join(timeout=1.0)
    if process.is_alive() and hasattr(process, "kill"):
        process.kill()
        process.join(timeout=1.0)


def _load_sdk() -> _BaoStockSessionSdkPort:
    try:
        import baostock
    except ImportError as exc:
        raise RuntimeError("dependency_unavailable") from exc
    return cast(_BaoStockSessionSdkPort, baostock)


def _dependency_versions() -> tuple[tuple[str, str], ...]:
    values: list[tuple[str, str]] = []
    for package in ("baostock", "pandas"):
        try:
            values.append((package, importlib.metadata.version(package)))
        except importlib.metadata.PackageNotFoundError:
            values.append((package, "not-installed"))
    return tuple(values)


def _silence_vendor_output() -> None:
    descriptor = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(descriptor, 1)
        os.dup2(descriptor, 2)
    finally:
        os.close(descriptor)


def _login(sdk: _BaoStockSessionSdkPort) -> None:
    try:
        # Match the working BaoStock integration: use its anonymous defaults.
        result = sdk.login()
    except PermissionError as exc:
        raise RuntimeError("supplier_login_network_denied") from exc
    except TimeoutError as exc:
        raise RuntimeError("supplier_login_timeout") from exc
    except UnboundLocalError as exc:
        # BaoStock <= 0.9.30 masks a socket-connect error with this bug.
        raise RuntimeError("supplier_login_transport_failed") from exc
    except OSError as exc:
        raise RuntimeError("supplier_login_network_failed") from exc
    except Exception as exc:
        raise RuntimeError("supplier_login_sdk_failed") from exc
    error_code = str(result.error_code)
    if error_code != "0":
        if error_code == "10001011":
            raise RuntimeError("supplier_login_failed_blacklisted")
        if error_code.isascii() and error_code.isalnum():
            raise RuntimeError(f"supplier_login_rejected_{error_code[:24]}")
        raise RuntimeError("supplier_login_rejected")


def _logout(sdk: _BaoStockSessionSdkPort) -> None:
    try:
        sdk.logout()
    except Exception:
        pass


def _available_disk_gb(path: Path) -> float:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return shutil.disk_usage(candidate).free / 1024**3


def _runtime_root(runtime_dir: Path, sessions: int) -> Path:
    return runtime_dir / "baostock-daily" / f"sessions-{sessions}"


def _process_group_rss_mb(handles: Sequence[_WorkerHandle]) -> float:
    child_rss = sum(_process_rss_mb(handle.process.pid) for handle in handles if handle.process.pid is not None)
    return _process_rss_mb(os.getpid()) + child_rss


def _process_rss_mb(pid: int) -> float:
    try:
        for line in Path(f"/proc/{pid}/status").read_text(encoding="ascii").splitlines():
            if line.startswith("VmRSS:"):
                return float(line.split()[1]) / 1024.0
    except (OSError, ValueError):
        return 0.0
    return 0.0


def _failure_code(exc: BaseException) -> str:
    message = str(exc)
    if _valid_failure_code(message):
        return message
    return type(exc).__name__.lower()[:48]


def _valid_failure_code(value: str) -> bool:
    return (
        0 < len(value) <= 64 and value.isascii() and all(character.isalnum() or character == "_" for character in value)
    )


def _emit_progress(
    progress: BaoStockRuntimeProgressPort | None,
    value: BaoStockRuntimeProgress,
) -> None:
    if progress is not None:
        progress.publish(value)


class _DownloadLock:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: IO[str] | None = None

    def __enter__(self) -> _DownloadLock:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self._path.open("a+")
        try:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:
            self._handle.close()
            self._handle = None
            raise
        except OSError as exc:
            self._handle.close()
            self._handle = None
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise BlockingIOError from exc
            raise RuntimeError("download_lock_unavailable") from exc
        return self

    def __exit__(self, *_: object) -> None:
        if self._handle is None:
            return
        try:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()


__all__ = ["inspect_baostock_history", "project_baostock_runtime_status", "run_baostock_history"]
