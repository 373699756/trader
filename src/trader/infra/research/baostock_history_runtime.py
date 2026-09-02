"""Bounded process supervision for the explicit BaoStock research download."""

from __future__ import annotations

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
from typing import IO, Protocol, cast

from trader.application.research.baostock_daily import BaoStockShardContext
from trader.application.research.baostock_history_runtime import (
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
    baostock_effective_facts_probe,
    build_historical_effective_facts_audit,
)
from trader.infra.research.baostock_daily import (
    BaoStockDailyArtifactConflictError,
    BaoStockDailyMergedArtifactStore,
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
BAOSTOCK_QUERY_INTERVAL_SECONDS = 1.0
BAOSTOCK_MAX_RSS_MB = 4096.0


class _BaoStockSessionSdkPort(BaoStockSdkPort, Protocol):
    def login(self, user_id: str = "anonymous", password: str = "123456") -> BaoStockRowResult: ...

    def logout(self) -> BaoStockRowResult: ...


@dataclass(frozen=True)
class _ContextResponse:
    context: BaoStockShardContext | None
    failure_reason: str = ""


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
    ) -> None:
        self.__version__ = sdk.__version__
        self._sdk = sdk
        self._monotonic = monotonic
        self._sleep = sleep
        self._last_query_started: float | None = None

    def query_trade_dates(self, *, start_date: str, end_date: str) -> BaoStockRowResult:
        self._wait()
        return self._sdk.query_trade_dates(start_date=start_date, end_date=end_date)

    def query_stock_basic(self) -> BaoStockRowResult:
        self._wait()
        return self._sdk.query_stock_basic()

    def query_history_k_data_plus(  # noqa: PLR0913 - exact third-party SDK signature
        self,
        code: str,
        fields: str,
        *,
        start_date: str,
        end_date: str,
        frequency: str,
        adjustflag: str,
    ) -> BaoStockRowResult:
        self._wait()
        return self._sdk.query_history_k_data_plus(
            code,
            fields,
            start_date=start_date,
            end_date=end_date,
            frequency=frequency,
            adjustflag=adjustflag,
        )

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
) -> BaoStockRuntimeStatus:
    request.validate(repository_root)
    if request.sessions == 2000 and _available_disk_gb(request.runtime_dir) < 30:
        return BaoStockRuntimeStatus(
            state="resource_blocked",
            sessions=request.sessions,
            failure_reasons=("disk_below_30gb",),
        )
    cancel = cancel_requested or (lambda: False)
    root = request.runtime_dir / "baostock-daily"
    try:
        with _DownloadLock(root / ".download.lock"):
            return _run_locked(request, root, cancel)
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


def inspect_baostock_history(runtime_dir: Path) -> BaoStockRuntimeStatus:
    root = runtime_dir / "baostock-daily"
    if not (root / "manifest.json").is_file():
        return BaoStockRuntimeStatus()
    try:
        store = BaoStockDailyMergedArtifactStore(root)
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
        shard_count=len(tuple(root.glob("shard-*.sqlite3"))),
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
) -> BaoStockRuntimeStatus:
    if (root / "manifest.json").is_file():
        store = BaoStockDailyMergedArtifactStore(root)
        manifest = store.verify()
        descriptor = store.describe_frozen_daily_input()
        if descriptor.requested_sessions != request.sessions:
            raise BaoStockDailyArtifactConflictError("BaoStock completed manifest uses different sessions")
        _seal_research_handoff(root, manifest)
        return inspect_baostock_history(request.runtime_dir)
    root.mkdir(parents=True, exist_ok=True)
    spec = BaoStockDailySpec(sessions=request.sessions)
    process_context = get_context("spawn")
    context, context_failure = _fetch_context(process_context, spec, request)
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
    bounded_context = BaoStockShardContext(context.calendar, universe, context.source_versions)
    return _DownloadCoordinator(
        _DownloadRun(process_context, request, spec, bounded_context, root, cancel_requested)
    ).run()


def _fetch_context(
    process_context: SpawnContext,
    spec: BaoStockDailySpec,
    request: BaoStockRuntimeRequest,
) -> tuple[BaoStockShardContext | None, str]:
    last_failure = "supplier_context_failed"
    for _ in range(request.retries + 1):
        parent, child = process_context.Pipe()
        process = process_context.Process(target=_context_worker_main, args=(child, spec))
        process.start()
        child.close()
        try:
            if not parent.poll(request.timeout_seconds):
                last_failure = "supplier_call_timeout"
                _terminate_process(process)
                continue
            response = parent.recv()
            if not isinstance(response, _ContextResponse):
                last_failure = "supplier_context_protocol_invalid"
                continue
            if response.context is not None:
                process.join(timeout=1.0)
                return response.context, ""
            last_failure = response.failure_reason or "supplier_context_failed"
            if last_failure == "dependency_unavailable":
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
        sdk = _load_sdk()
        _login(sdk)
        gateway = BaoStockRowGateway(
            _RateLimitedBaoStockSdk(sdk),
            dependency_versions=_dependency_versions(),
        )
        connection.send(
            _ContextResponse(
                BaoStockShardContext(
                    gateway.fetch_calendar(spec),
                    gateway.fetch_universe(spec),
                    gateway.source_versions(),
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


class _DownloadCoordinator:
    def __init__(self, run: _DownloadRun) -> None:
        self._run = run
        self._shards = tuple(
            SQLiteBaoStockDailyShard(run.root / f"shard-{index:02d}.sqlite3") for index in range(run.request.workers)
        )
        self._pending: deque[BaoStockSecurity] = deque()
        self._attempts: dict[str, int] = {}
        self._terminal_failures: dict[str, str] = {}
        self._handles: list[_WorkerHandle] = []
        self._peak_rss_mb = 0.0
        self._cancelling_since: float | None = None
        self._resource_blocked = False

    def run(self) -> BaoStockRuntimeStatus:
        self._initialize_shards()
        startup_failure = self._start_workers()
        if not self._handles:
            return self._partial("failed", (startup_failure or "worker_start_failed",))
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
            shard.initialize(run.spec, run.context.calendar, run.context.universe, run.context.source_versions)
        completed = _completed_codes(self._shards, run.spec)
        self._pending.extend(item for item in run.context.universe if item.code not in completed)

    def _start_workers(self) -> str:
        failure = ""
        for shard in self._shards:
            handle, failure = _start_worker(
                self._run.process_context,
                self._run.spec,
                self._run.context,
                shard.path,
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
            self._peak_rss_mb = max(self._peak_rss_mb, _process_group_rss_mb(self._handles))
            if self._peak_rss_mb > BAOSTOCK_MAX_RSS_MB:
                self._resource_blocked = True
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
            self._accept_response(handle)
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

    def _accept_response(self, handle: _WorkerHandle) -> None:
        security = self._release(handle)
        try:
            response = handle.connection.recv()
        except (EOFError, OSError):
            response = _DownloadResponse(security.code, False, "worker_process_failed")
        if not isinstance(response, _DownloadResponse) or response.code != security.code:
            self._retry_or_record(security, "worker_protocol_invalid")
        elif not response.succeeded:
            self._retry_or_record(security, response.failure_reason or "supplier_query_failed")
        else:
            self._attempts.pop(security.code, None)
            self._terminal_failures.pop(security.code, None)
            for shard in self._shards:
                shard.clear_failure(self._run.spec, security.code)

    def _retry_or_record(self, security: BaoStockSecurity, reason: str) -> None:
        failures = self._attempts.get(security.code, 0) + 1
        self._attempts[security.code] = failures
        if failures <= self._run.request.retries:
            self._pending.appendleft(security)
            return
        bounded_reason = reason if _valid_failure_code(reason) else "supplier_query_failed"
        self._terminal_failures[security.code] = bounded_reason
        self._failure_shard(security).record_failure(self._run.spec, security.code, bounded_reason)

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
        return self._shards[_shard_index(security.code, len(self._shards))]

    def _finish(self) -> BaoStockRuntimeStatus:
        if self._resource_blocked:
            return self._partial("resource_blocked", ("rss_above_4gb",))
        if self._cancelling_since is not None:
            return self._partial("cancelled", ("cancelled",))
        if self._terminal_failures:
            return self._partial("completed_with_failures", tuple(self._terminal_failures.values()))
        snapshots = tuple(shard.snapshot(self._run.spec) for shard in self._shards)
        completed = frozenset(item.code for snapshot in snapshots for item in snapshot.batches)
        if completed != frozenset(item.code for item in self._run.context.universe):
            return self._partial("completed_with_failures", ("incomplete_codes",))
        return self._publish(snapshots, completed)

    def _publish(
        self,
        snapshots: tuple[BaoStockShardSnapshot, ...],
        completed: frozenset[str],
    ) -> BaoStockRuntimeStatus:
        merged = BaoStockDailyMergedArtifactStore(self._run.root).write(self._run.spec, snapshots)
        facts, dataset = _seal_research_handoff(self._run.root, merged)
        audit = merged.audit
        return BaoStockRuntimeStatus(
            state="completed" if audit.status == "coverage_ready" else "completed_with_failures",
            sessions=self._run.request.sessions,
            shard_count=len(self._shards),
            universe_count=len(self._run.context.universe),
            completed_codes=len(completed),
            failed_codes=len(audit.failed_codes),
            peak_rss_mb=self._peak_rss_mb,
            manifest_hash=merged.content_hash,
            coverage_status=audit.status,
            historical_effective_facts_status=facts.status,
            historical_effective_facts_hash=facts.content_hash,
            v3_dataset_status=dataset.status,
            v3_dataset_hash=dataset.content_hash,
            failure_reasons=audit.failure_reasons,
        )

    def _partial(self, state: BaoStockRuntimeState, reasons: tuple[str, ...]) -> BaoStockRuntimeStatus:
        completed = _completed_codes(self._shards, self._run.spec)
        return BaoStockRuntimeStatus(
            state=state,
            sessions=self._run.request.sessions,
            shard_count=len(self._shards),
            universe_count=len(self._run.context.universe),
            completed_codes=len(completed),
            failed_codes=len(self._run.context.universe) - len(completed),
            peak_rss_mb=self._peak_rss_mb,
            failure_reasons=tuple(reasons or ("incomplete_codes",)),
        )


def _seal_research_handoff(
    root: Path,
    daily: BaoStockDailyManifest,
) -> tuple[HistoricalEffectiveFactsAudit, BaoStockV3DatasetManifest]:
    facts = HistoricalEffectiveFactsArtifactStore(root).write(
        build_historical_effective_facts_audit((baostock_effective_facts_probe(),))
    )
    dataset = BaoStockV3DatasetArtifactStore(root).write(build_baostock_v3_dataset_manifest(daily, facts, ()))
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
                if last_failure == "dependency_unavailable":
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
        archive = SQLiteBaoStockDailyShard(Path(shard_path))
        archive.initialize(spec, context.calendar, context.universe, context.source_versions)
        sdk = _load_sdk()
        _login(sdk)
        gateway = BaoStockRowGateway(
            _RateLimitedBaoStockSdk(sdk),
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
                batch = gateway.fetch_code_batch(spec, command.security, context.calendar)
                archive.save_batch(spec, batch)
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


def _completed_codes(
    shards: tuple[SQLiteBaoStockDailyShard, ...],
    spec: BaoStockDailySpec,
) -> frozenset[str]:
    values: set[str] = set()
    for shard in shards:
        values.update(shard.completed_codes(spec))
    return frozenset(values)


def _shard_index(code: str, shard_count: int) -> int:
    return int(code) % shard_count


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
    configured_user_id = os.environ.get("BAOSTOCK_USER_ID")
    configured_password = os.environ.get("BAOSTOCK_PASSWORD")
    configured_api_key = os.environ.get("BAOSTOCK_API_KEY")
    user_id = (configured_user_id if configured_user_id is not None else "anonymous").strip()
    password = configured_password if configured_password is not None else "123456"
    if not user_id or not password:
        raise RuntimeError("supplier_login_credentials_missing")
    api_key = (configured_api_key or "").strip()
    if api_key:
        setter = getattr(sdk, "set_API_key", None)
        if not callable(setter):
            raise RuntimeError("supplier_api_key_unsupported")
        try:
            setter(api_key)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise RuntimeError("supplier_api_key_rejected") from exc
    try:
        # Preserve BaoStock's proven anonymous entrypoint when no credentials
        # were configured; some SDK-compatible ports only implement login().
        if configured_user_id is None and configured_password is None:
            result = sdk.login()
        else:
            result = sdk.login(user_id=user_id, password=password)
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
        except (ImportError, OSError) as exc:
            self._handle.close()
            self._handle = None
            raise BlockingIOError from exc
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
