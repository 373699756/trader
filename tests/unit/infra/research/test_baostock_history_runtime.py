from __future__ import annotations

import errno
import fcntl
import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from trader.application.research.baostock_daily import BaoStockShardContext
from trader.application.research.baostock_history_runtime import (
    BaoStockRuntimeProgress,
    BaoStockRuntimeRequest,
    BaoStockRuntimeStatus,
)
from trader.domain.research.baostock_daily import (
    BAOSTOCK_LEGACY_CALENDAR_SCHEMA,
    BaoStockCalendar,
    BaoStockDailySpec,
    BaoStockSecurity,
    BaoStockSourceVersions,
)
from trader.infra.research.baostock_daily import SQLiteBaoStockDailyShard
from trader.infra.research.baostock_history_runtime import (
    _ContextResponse,
    _ContextStage,
    _DownloadCoordinator,
    _DownloadLock,
    _DownloadRun,
    _fetch_context,
    _load_resume_context,
    _partition_name,
    _quarantine_corrupt_archive_parts,
    _run_locked,
    _SupplierCallActivity,
    _WorkerHandle,
    run_baostock_history,
)


@pytest.mark.parametrize(
    ("board", "code", "expected"),
    (
        ("main", "600001", "main-6000.sqlite3"),
        ("chinext", "300750", "chinext-3007.sqlite3"),
        ("star", "688981", "star-6889.sqlite3"),
    ),
)
def test_partition_name_is_human_readable_and_bounds_each_database_to_one_hundred_codes(
    board: str, code: str, expected: str
) -> None:
    assert _partition_name(board, code) == expected


class _ProgressRecorder:
    def __init__(self) -> None:
        self.values: list[BaoStockRuntimeProgress] = []

    def publish(self, progress: BaoStockRuntimeProgress) -> None:
        self.values.append(progress)


def _coordinator(tmp_path: Path) -> tuple[_DownloadCoordinator, BaoStockSecurity, _ProgressRecorder]:
    spec = BaoStockDailySpec(sessions=1)
    calendar = BaoStockCalendar((spec.source_cutoff,))
    security = BaoStockSecurity(
        "600001",
        "fixture",
        "main",
        spec.source_cutoff - timedelta(days=365),
        None,
        "0.9.30",
    )
    context = BaoStockShardContext(
        calendar,
        (security,),
        BaoStockSourceVersions("0.9.30", "3.14.0", (("pandas", "2.3.0"),)),
    )
    recorder = _ProgressRecorder()
    run = _DownloadRun(
        process_context=None,  # type: ignore[arg-type] -- no process is started by these focused tests
        request=BaoStockRuntimeRequest(tmp_path, sessions=1, retries=2),
        spec=spec,
        context=context,
        root=tmp_path / "baostock-daily",
        cancel_requested=lambda: False,
        progress=recorder,
    )
    return _DownloadCoordinator(run), security, recorder


def test_blacklist_is_a_run_level_failure_instead_of_queuing_thousands_of_retries(tmp_path: Path) -> None:
    coordinator, security, _ = _coordinator(tmp_path)
    coordinator._initialize_shards()
    coordinator._pending.clear()

    coordinator._retry_or_record(security, "supplier_query_failed_blacklisted")
    status = coordinator._finish()

    assert status.state == "failed"
    assert status.failed_codes == 1
    assert status.completed_codes == 0
    assert status.failure_reasons == ("supplier_query_failed_blacklisted",)
    assert not coordinator._pending
    assert security.code not in coordinator._attempts


def test_progress_uses_checkpoint_database_as_resume_source(tmp_path: Path) -> None:
    coordinator, _, recorder = _coordinator(tmp_path)
    coordinator._initialize_shards()
    coordinator._report("downloading")

    progress = recorder.values[-1]
    assert progress.universe_count == 1
    assert progress.expected_records == 1
    assert progress.downloaded_records == 0
    assert progress.completed_codes == 0
    assert progress.failed_codes == 0


def test_partial_status_refreshes_checkpoints_committed_outside_parent_response(tmp_path: Path) -> None:
    coordinator, security, recorder = _coordinator(tmp_path)
    coordinator._initialize_shards()
    coordinator._failure_shard(security).record_failure(
        coordinator._run.spec,
        security.code,
        "supplier_query_failed_blacklisted",
    )

    status = coordinator._partial("failed", ("supplier_query_failed_blacklisted",))

    assert status.failed_codes == 1
    assert recorder.values[-1].failed_codes == 1
    assert recorder.values[-1].active_workers == 0
    assert recorder.values[-1].last_failure_reason == "supplier_query_failed_blacklisted"


def test_download_lock_distinguishes_an_active_owner_from_unsupported_locking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_with(error_number: int):
        def failing_flock(*_args: object) -> None:
            raise OSError(error_number, "bounded lock failure")

        return failing_flock

    monkeypatch.setattr(fcntl, "flock", fail_with(errno.EAGAIN))
    with pytest.raises(BlockingIOError):
        with _DownloadLock(tmp_path / "busy.lock"):
            pass

    monkeypatch.setattr(fcntl, "flock", fail_with(errno.ENOTSUP))
    with pytest.raises(RuntimeError, match="download_lock_unavailable"):
        with _DownloadLock(tmp_path / "unsupported.lock"):
            pass


def test_resume_loads_frozen_calendar_and_universe_without_refetching_supplier_context(tmp_path: Path) -> None:
    coordinator, _, _ = _coordinator(tmp_path)
    coordinator._initialize_shards()
    spec = coordinator._run.spec

    resumed = _load_resume_context(coordinator._run.root, spec)

    assert resumed == coordinator._run.context
    shard = SQLiteBaoStockDailyShard(coordinator._run.root / "shards" / "main-6000.sqlite3")
    assert shard.context(spec) == resumed


def test_resume_loads_legacy_calendar_context_without_supplier_fetch(tmp_path: Path) -> None:
    spec = BaoStockDailySpec(sessions=1)
    calendar = replace(BaoStockCalendar((spec.source_cutoff,)), schema_version=BAOSTOCK_LEGACY_CALENDAR_SCHEMA)
    security = BaoStockSecurity(
        "600001",
        "fixture",
        "main",
        spec.source_cutoff - timedelta(days=365),
        None,
        "0.9.30",
    )
    context = BaoStockShardContext(calendar, (security,), BaoStockSourceVersions("0.9.30", "3.14.0", ()))
    root = tmp_path / "baostock-daily" / "sessions-1"
    shard = SQLiteBaoStockDailyShard(root / "shards" / "main-6000.sqlite3")
    shard.initialize(spec, context.calendar, context.universe, context.source_versions)

    resumed = _load_resume_context(root, spec)

    assert resumed == context
    assert resumed is not None
    assert resumed.calendar.schema_version == BAOSTOCK_LEGACY_CALENDAR_SCHEMA


def test_coordinator_creates_human_readable_partition_databases_instead_of_worker_buckets(tmp_path: Path) -> None:
    coordinator, _, _ = _coordinator(tmp_path)

    coordinator._initialize_shards()

    assert (coordinator._run.root / "shards" / "main-6000.sqlite3").is_file()
    assert not (coordinator._run.root / "shard-00.sqlite3").exists()


def test_corrupt_partition_is_quarantined_without_removing_healthy_shards(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    shards = root / "shards"
    shards.mkdir(parents=True)
    healthy = shards / "main-6000.sqlite3"
    corrupt = shards / "main-6001.sqlite3"
    healthy.write_bytes(b"healthy")
    corrupt.write_bytes(b"corrupt")
    healthy_digest = hashlib.sha256(healthy.read_bytes()).hexdigest()
    (root / "catalog.sqlite3").write_bytes(b"catalog")
    catalog_digest = hashlib.sha256((root / "catalog.sqlite3").read_bytes()).hexdigest()
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "catalog_sha256": catalog_digest,
                "partitions": [
                    {"relative_path": "shards/main-6000.sqlite3", "database_sha256": healthy_digest},
                    {"relative_path": "shards/main-6001.sqlite3", "database_sha256": "0" * 64},
                ],
            }
        ),
        encoding="utf-8",
    )

    _quarantine_corrupt_archive_parts(root)

    assert healthy.is_file()
    assert not corrupt.exists()
    quarantine = tuple((root / "quarantine").glob("recovery-*"))
    assert len(quarantine) == 1
    assert (quarantine[0] / corrupt.name).is_file()
    assert (quarantine[0] / "manifest.json").is_file()
    assert (quarantine[0] / "catalog.sqlite3").is_file()


def test_resume_reports_persisted_totals_before_starting_a_supplier_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator, _, recorder = _coordinator(tmp_path)
    coordinator._initialize_shards()

    def supplier_context_must_not_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("resume must not refetch supplier context")

    monkeypatch.setattr(
        "trader.infra.research.baostock_history_runtime._fetch_context",
        supplier_context_must_not_run,
    )
    monkeypatch.setattr(
        "trader.infra.research.baostock_history_runtime._start_worker",
        lambda *_args, **_kwargs: (None, "worker_start_failed"),
    )

    status = _run_locked(coordinator._run.request, coordinator._run.root, lambda: False, recorder)

    assert status.state == "failed"
    assert [value.phase for value in recorder.values[-4:-1]] == [
        "checkpoint_loading",
        "database_initializing",
        "worker_starting",
    ]
    assert recorder.values[-1].phase == "worker_starting"
    assert recorder.values[-1].last_failure_reason == "worker_start_failed"
    assert recorder.values[-1].universe_count == 1
    assert recorder.values[-1].expected_records == 1


def test_context_query_blacklist_stops_without_retrying(tmp_path: Path) -> None:
    class _Connection:
        def __init__(self, response: object) -> None:
            self._response = response

        def poll(self, _timeout: float) -> bool:
            return True

        def recv(self) -> object:
            return self._response

        def close(self) -> None:
            pass

    class _Process:
        def start(self) -> None:
            pass

        def is_alive(self) -> bool:
            return False

        def join(self, timeout: float | None = None) -> None:
            pass

    class _ProcessContext:
        def __init__(self) -> None:
            self.process_count = 0

        def Pipe(self):
            response = _ContextResponse(None, "supplier_query_failed_blacklisted")
            return _Connection(response), _Connection(response)

        def Process(self, *, target, args):
            self.process_count += 1
            return _Process()

    process_context = _ProcessContext()
    request = BaoStockRuntimeRequest(tmp_path, sessions=1, retries=2)

    context, failure = _fetch_context(  # type: ignore[arg-type] -- bounded multiprocessing test double
        process_context,
        BaoStockDailySpec(sessions=1),
        request,
        None,
    )

    assert context is None
    assert failure == "supplier_query_failed_blacklisted"
    assert process_context.process_count == 1


def test_context_watchdog_allows_many_bounded_supplier_calls_in_one_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _coordinator(tmp_path)[0]._run.context
    responses = [
        _ContextStage("supplier_login"),
        _SupplierCallActivity("started"),
        _SupplierCallActivity("completed"),
        _ContextStage("trading_calendar"),
        _SupplierCallActivity("started"),
        _SupplierCallActivity("completed"),
        _ContextStage("security_universe"),
        *(_SupplierCallActivity(state) for _ in range(18) for state in ("started", "completed")),
        _ContextResponse(context),
    ]

    class _Connection:
        def __init__(self) -> None:
            self._responses = iter(responses)

        def poll(self, timeout: float) -> bool:
            assert 0 < timeout <= 60.0
            return True

        def recv(self) -> object:
            return next(self._responses)

        def close(self) -> None:
            pass

    class _Process:
        def start(self) -> None:
            pass

        def is_alive(self) -> bool:
            return False

        def join(self, timeout: float | None = None) -> None:
            pass

    connection = _Connection()

    class _ProcessContext:
        def Pipe(self):
            return connection, connection

        def Process(self, *, target, args):
            return _Process()

    now = iter(float(value * 30) for value in range(len(responses) * 2 + 2))
    monkeypatch.setattr("trader.infra.research.baostock_history_runtime.time.monotonic", lambda: next(now))

    actual, failure = _fetch_context(  # type: ignore[arg-type] -- bounded multiprocessing test double
        _ProcessContext(),
        BaoStockDailySpec(sessions=1),
        BaoStockRuntimeRequest(tmp_path, sessions=1, retries=0),
        None,
    )

    assert actual == context
    assert failure == ""


def test_download_worker_activity_refreshes_watchdog_without_finishing_the_security(tmp_path: Path) -> None:
    coordinator, security, _ = _coordinator(tmp_path)

    class _Connection:
        def recv(self) -> object:
            return _SupplierCallActivity("completed")

    class _Process:
        pass

    handle = _WorkerHandle(  # type: ignore[arg-type] -- focused process/connection doubles
        process=_Process(),
        connection=_Connection(),
        shard_path=tmp_path,
        current=security,
        started_at=1.0,
    )

    coordinator._accept_response(handle, now=59.0)

    assert handle.current == security
    assert handle.started_at == 59.0
    assert coordinator._attempts == {}
    assert coordinator._terminal_failures == {}


def test_download_worker_pipe_failure_is_recorded_without_releasing_the_security_twice(tmp_path: Path) -> None:
    coordinator, security, _ = _coordinator(tmp_path)

    class _Connection:
        def recv(self) -> object:
            raise EOFError

    class _Process:
        pass

    handle = _WorkerHandle(  # type: ignore[arg-type] -- focused process/connection doubles
        process=_Process(),
        connection=_Connection(),
        shard_path=tmp_path,
        current=security,
        started_at=1.0,
    )

    coordinator._accept_response(handle, now=2.0)

    assert handle.current is None
    assert tuple(coordinator._pending) == (security,)
    assert coordinator._attempts == {security.code: 1}


def test_run_projects_unsupported_download_lock_as_a_controlled_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unsupported_flock(*_args: object) -> None:
        raise OSError(errno.ENOTSUP, "bounded lock failure")

    monkeypatch.setattr(fcntl, "flock", unsupported_flock)

    status = run_baostock_history(BaoStockRuntimeRequest(tmp_path, sessions=1), tmp_path.parent)

    assert status.state == "failed"
    assert status.failure_reasons == ("download_lock_unavailable",)


def test_full_download_allows_exactly_25_gib_before_supplier_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("trader.infra.research.baostock_history_runtime._available_disk_gb", lambda _path: 25.0)
    monkeypatch.setattr(
        "trader.infra.research.baostock_history_runtime._run_locked",
        lambda request, _root, _cancel, _progress: BaoStockRuntimeStatus(
            state="cancelled", sessions=request.sessions, failure_reasons=("cancelled",)
        ),
    )

    status = run_baostock_history(BaoStockRuntimeRequest(tmp_path, sessions=2000), tmp_path.parent)

    assert status.state == "cancelled"


def test_full_download_blocks_below_25_gib_before_supplier_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("trader.infra.research.baostock_history_runtime._available_disk_gb", lambda _path: 24.99)

    status = run_baostock_history(BaoStockRuntimeRequest(tmp_path, sessions=2000), tmp_path.parent)

    assert status.state == "resource_blocked"
    assert status.failure_reasons == ("disk_below_25gb",)


def test_session_isolated_root_does_not_load_a_one_day_pilot_for_a_full_run(tmp_path: Path) -> None:
    coordinator, _, _ = _coordinator(tmp_path)
    coordinator._initialize_shards()

    full_root = tmp_path / "baostock-daily" / "sessions-2000"

    assert not full_root.exists()


def test_history_download_rejects_multiple_workers(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="exactly one worker"):
        BaoStockRuntimeRequest(tmp_path, sessions=2000, workers=2).validate(tmp_path.parent)


def test_low_disk_watermark_stops_after_the_current_stock_checkpoint(tmp_path: Path) -> None:
    coordinator, _, _ = _coordinator(tmp_path)
    coordinator._initialize_shards()
    coordinator._resource_blocked = True
    coordinator._resource_block_reason = "disk_low_watermark"

    status = coordinator._finish()

    assert status.state == "resource_blocked"
    assert status.failure_reasons == ("disk_low_watermark",)
