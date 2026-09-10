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
    BaoStockDailyJoinRequest,
    BaoStockDailyManifest,
    BaoStockDailySide,
    BaoStockDailySpec,
    BaoStockIndustryInterval,
    BaoStockSecurity,
    BaoStockSourceVersions,
    join_baostock_daily_sides,
)
from trader.infra.research.baostock_catalog import partition_name
from trader.infra.research.baostock_daily import BaoStockDailyPartitionedArchive, SQLiteBaoStockDailyShard
from trader.infra.research.baostock_history_legacy import legacy_daily_database_sha256, migrate_legacy_archive
from trader.infra.research.baostock_history_runtime import (
    _ContextResponse,
    _ContextStage,
    _DownloadCoordinator,
    _DownloadLock,
    _DownloadResponse,
    _DownloadRun,
    _failure_code,
    _fetch_context,
    _load_resume_context,
    _quarantine_corrupt_archive_parts,
    _run_locked,
    _SupplierCallActivity,
    _WorkerHandle,
    inspect_baostock_history,
    project_baostock_runtime_status,
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
    assert partition_name(board, code) == expected


class _ProgressRecorder:
    def __init__(self) -> None:
        self.values: list[BaoStockRuntimeProgress] = []

    def publish(self, progress: BaoStockRuntimeProgress) -> None:
        self.values.append(progress)


def test_runtime_status_projection_explicitly_publishes_training_readiness() -> None:
    projected = project_baostock_runtime_status(
        BaoStockRuntimeStatus(
            universe_count=2,
            completed_codes=2,
            training_ready_codes=1,
        )
    )

    assert projected["completed_codes"] == 2
    assert projected["training_ready_codes"] == 1


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


def _batch_for_security(security: BaoStockSecurity, spec: BaoStockDailySpec):
    day = spec.source_cutoff

    def side(adjustment: str) -> BaoStockDailySide:
        return BaoStockDailySide(
            security.code,
            day,
            adjustment,
            10.0,
            10.5,
            9.8,
            10.2,
            100.0,
            1_000.0,
            9.9 if adjustment == "unadjusted" else None,
            3.03 if adjustment == "unadjusted" else None,
            1.2 if adjustment == "unadjusted" else None,
            "trading",
        )

    return join_baostock_daily_sides(
        BaoStockDailyJoinRequest(security.code, (day,), spec.source_cutoff),
        (side("unadjusted"),),
        (side("qfq"),),
    )


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


def test_resume_progress_reads_checkpoint_index_without_snapshot_payload_decode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator, security, recorder = _coordinator(tmp_path)
    coordinator._initialize_shards()
    shard = coordinator._failure_shard(security)
    shard.record_failure(coordinator._run.spec, security.code, "supplier_query_failed")

    def snapshot_must_not_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("resume progress must not decode daily payloads")

    monkeypatch.setattr(shard, "snapshot", snapshot_must_not_run)
    coordinator._refresh_checkpoint_progress()

    assert coordinator._failed_codes == {security.code}
    assert coordinator._downloaded_records == 0


def test_finish_uses_checkpoint_indexes_and_streamed_partition_sealing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator, security, _ = _coordinator(tmp_path)
    coordinator._initialize_shards()
    shard = coordinator._failure_shard(security)
    shard.save_batch(coordinator._run.spec, _batch_for_security(security, coordinator._run.spec))

    def snapshot_must_not_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("finish must not decode every partition before sealing")

    monkeypatch.setattr(shard, "snapshot", snapshot_must_not_run)

    status = coordinator._finish()

    assert status.completed_codes == 1
    assert status.training_ready_codes == 0
    assert status.manifest_hash


def test_completed_daily_batch_with_incomplete_industry_is_not_downloaded_again(tmp_path: Path) -> None:
    coordinator, security, _ = _coordinator(tmp_path)
    coordinator._initialize_shards()
    coordinator._pending.clear()
    shard = coordinator._failure_shard(security)
    shard.save_batch(coordinator._run.spec, _batch_for_security(security, coordinator._run.spec))

    coordinator._initialize_shards()

    assert not coordinator._pending
    assert coordinator._terminal_failures == {security.code: "historical_industry_incomplete"}
    assert coordinator._completed_codes == {security.code}
    assert coordinator._ready_codes == set()
    assert coordinator._failed_codes == set()


def test_completed_daily_batch_with_partially_usable_industry_queues_facts_only(tmp_path: Path) -> None:
    base, security, recorder = _coordinator(tmp_path)
    first_day = base._run.spec.source_cutoff - timedelta(days=1)
    calendar = BaoStockCalendar((first_day, base._run.spec.source_cutoff))
    spec = BaoStockDailySpec(sessions=2)
    partial = (
        BaoStockIndustryInterval(
            security.code,
            spec.source_cutoff,
            None,
            "银行",
            "申万一级行业",
        ),
    )
    context = BaoStockShardContext(calendar, (security,), base._run.context.source_versions, partial)
    run = replace(
        base._run,
        request=replace(base._run.request, sessions=2),
        spec=spec,
        context=context,
        root=tmp_path / "partial-industry",
        progress=recorder,
    )
    coordinator = _DownloadCoordinator(run)
    coordinator._initialize_shards()
    shard = coordinator._failure_shard(security)
    shard.save_batch(
        spec,
        join_baostock_daily_sides(
            BaoStockDailyJoinRequest(security.code, calendar.open_dates, spec.source_cutoff),
            tuple(
                BaoStockDailySide(
                    security.code, day, "unadjusted", 10.0, 10.5, 9.8, 10.2, 100.0, 1_000.0, 9.9, 3.03, 1.2, "trading"
                )
                for day in calendar.open_dates
            ),
            tuple(
                BaoStockDailySide(
                    security.code, day, "qfq", 10.0, 10.5, 9.8, 10.2, 100.0, 1_000.0, None, None, None, "trading"
                )
                for day in calendar.open_dates
            ),
        ),
    )

    coordinator._pending.clear()
    coordinator._initialize_shards()

    assert tuple(coordinator._pending) == (security,)
    assert coordinator._terminal_failures == {}
    assert coordinator._completed_codes == {security.code}
    assert coordinator._ready_codes == set()


def test_daily_archive_seals_independently_and_keeps_industry_training_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, security, _ = _coordinator(tmp_path)
    coordinator = _DownloadCoordinator(replace(base._run, root=tmp_path / "baostock-daily" / "sessions-1"))
    verification_calls = 0
    original_verify = BaoStockDailyPartitionedArchive.verify

    def _counted_verify(archive: BaoStockDailyPartitionedArchive) -> BaoStockDailyManifest:
        nonlocal verification_calls
        verification_calls += 1
        return original_verify(archive)

    monkeypatch.setattr(BaoStockDailyPartitionedArchive, "verify", _counted_verify)
    coordinator._initialize_shards()
    coordinator._failure_shard(security).save_batch(
        coordinator._run.spec,
        join_baostock_daily_sides(
            BaoStockDailyJoinRequest(
                security.code,
                (coordinator._run.spec.source_cutoff,),
                coordinator._run.spec.source_cutoff,
                2,
            ),
            (),
            (),
        ),
    )
    coordinator._initialize_shards()

    status = coordinator._finish()

    assert status.state == "completed_with_failures"
    assert status.completed_codes == 1
    assert status.failed_codes == 0
    assert status.training_ready_codes == 0
    assert status.coverage_status == "historical_data_insufficient"
    assert status.historical_effective_facts_status == "historical_data_insufficient"
    assert "historical_industry_effective_at_unavailable" in status.failure_reasons
    assert (coordinator._run.root / "manifest.json").is_file()
    assert verification_calls == 1

    inspected = inspect_baostock_history(tmp_path, sessions=1)
    assert inspected.completed_codes == 1
    assert inspected.training_ready_codes == 0
    assert inspected.historical_effective_facts_status == "historical_data_insufficient"
    assert verification_calls == 1


def test_incomplete_historical_industry_has_stable_failure_code() -> None:
    assert _failure_code(ValueError("BaoStock historical industry does not cover every expected date")) == (
        "historical_industry_incomplete"
    )


def test_industry_incomplete_response_counts_daily_download_without_marking_code_failed(tmp_path: Path) -> None:
    coordinator, security, recorder = _coordinator(tmp_path)
    coordinator._initialize_shards()

    class _Connection:
        def recv(self) -> object:
            return _DownloadResponse(
                security.code,
                True,
                "historical_industry_incomplete",
                training_ready=False,
            )

    class _Process:
        def is_alive(self) -> bool:
            return True

    handle = _WorkerHandle(  # type: ignore[arg-type] -- focused process/connection doubles
        process=_Process(),
        connection=_Connection(),
        shard_path=tmp_path,
        current=security,
        started_at=1.0,
    )

    coordinator._accept_response(handle, now=2.0)

    assert coordinator._completed_codes == {security.code}
    assert coordinator._ready_codes == set()
    assert coordinator._failed_codes == set()
    assert coordinator._terminal_failures == {security.code: "historical_industry_incomplete"}
    assert recorder.values[-1].downloaded_records == 1
    assert recorder.values[-1].last_failure_reason == ""


def test_training_fact_only_response_does_not_double_count_durable_daily_rows(tmp_path: Path) -> None:
    coordinator, security, recorder = _coordinator(tmp_path)
    coordinator._initialize_shards()
    coordinator._completed_codes.add(security.code)
    coordinator._downloaded_records = 1

    class _Connection:
        def recv(self) -> object:
            return _DownloadResponse(security.code, True, daily_downloaded=False)

    class _Process:
        def is_alive(self) -> bool:
            return True

    handle = _WorkerHandle(  # type: ignore[arg-type] -- focused process/connection doubles
        process=_Process(),
        connection=_Connection(),
        shard_path=tmp_path,
        current=security,
        started_at=1.0,
    )

    coordinator._accept_response(handle, now=2.0)

    assert coordinator._ready_codes == {security.code}
    assert coordinator._downloaded_records == 1
    assert recorder.values[-1].training_ready_codes == 1


def test_success_clears_only_the_codes_owning_partition(tmp_path: Path) -> None:
    coordinator, security, _ = _coordinator(tmp_path)
    coordinator._initialize_shards()

    class _Connection:
        def recv(self) -> object:
            return _DownloadResponse(security.code, True)

    class _Process:
        def is_alive(self) -> bool:
            return True

    class _UnrelatedShard:
        def clear_failure(self, *_args: object) -> None:
            raise AssertionError("unrelated partitions must not decode their full context")

    coordinator._shards = (*coordinator._shards, _UnrelatedShard())  # type: ignore[assignment]
    handle = _WorkerHandle(  # type: ignore[arg-type] -- focused process/connection doubles
        process=_Process(),
        connection=_Connection(),
        shard_path=tmp_path,
        current=security,
        started_at=1.0,
    )

    coordinator._accept_response(handle, now=2.0)

    assert coordinator._completed_codes == {security.code}


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


def test_inspection_reports_in_progress_checkpoint_counts_without_a_manifest(tmp_path: Path) -> None:
    coordinator, security, _ = _coordinator(tmp_path)
    spec = coordinator._run.spec
    context = coordinator._run.context
    root = tmp_path / "baostock-daily" / "sessions-1"
    shard = SQLiteBaoStockDailyShard(root / "shards" / "main-6000.sqlite3")
    shard.initialize(
        spec,
        context.calendar,
        context.universe,
        context.source_versions,
        context.industry_intervals,
    )
    shard.save_batch(spec, _batch_for_security(security, spec))

    status = inspect_baostock_history(tmp_path, sessions=1)

    assert status.state == "completed_with_failures"
    assert status.universe_count == 1
    assert status.completed_codes == 1
    assert status.training_ready_codes == 0
    assert status.failure_reasons == ("incomplete_codes",)


def test_inspection_preserves_checkpoint_counts_when_manifest_is_invalid(tmp_path: Path) -> None:
    coordinator, security, _ = _coordinator(tmp_path)
    spec = coordinator._run.spec
    context = coordinator._run.context
    root = tmp_path / "baostock-daily" / "sessions-1"
    shard = SQLiteBaoStockDailyShard(root / "shards" / "main-6000.sqlite3")
    shard.initialize(spec, context.calendar, context.universe, context.source_versions)
    shard.save_batch(spec, _batch_for_security(security, spec))
    (root / "manifest.json").write_text("{}", encoding="utf-8")

    status = inspect_baostock_history(tmp_path, sessions=1)

    assert status.state == "failed"
    assert status.universe_count == 1
    assert status.completed_codes == 1
    assert status.failure_reasons == ("manifest_invalid",)


def test_inspection_keeps_root_level_legacy_checkpoints_visible_before_migration(tmp_path: Path) -> None:
    coordinator, security, _ = _coordinator(tmp_path)
    spec = coordinator._run.spec
    context = coordinator._run.context
    root = tmp_path / "baostock-daily" / "sessions-1"
    shard = SQLiteBaoStockDailyShard(root / "shard-000.sqlite3")
    shard.initialize(spec, context.calendar, context.universe, context.source_versions)
    shard.save_batch(spec, _batch_for_security(security, spec))

    status = inspect_baostock_history(tmp_path, sessions=1)

    assert status.state == "completed_with_failures"
    assert status.shard_count == 1
    assert status.universe_count == 1
    assert status.completed_codes == 1
    assert status.training_ready_codes == 0


def test_resume_context_loads_from_root_level_legacy_checkpoint_without_supplier_fetch(tmp_path: Path) -> None:
    coordinator, _, _ = _coordinator(tmp_path)
    spec = coordinator._run.spec
    context = coordinator._run.context
    root = tmp_path / "baostock-daily" / "sessions-1"
    legacy = SQLiteBaoStockDailyShard(root / "shard-000.sqlite3")
    legacy.initialize(spec, context.calendar, context.universe, context.source_versions)

    resumed = _load_resume_context(root, spec)

    assert resumed == context


def test_root_level_legacy_checkpoints_migrate_into_partitions_without_data_loss(tmp_path: Path) -> None:
    coordinator, security, _ = _coordinator(tmp_path)
    spec = coordinator._run.spec
    context = coordinator._run.context
    root = tmp_path / "baostock-daily" / "sessions-1"
    legacy = SQLiteBaoStockDailyShard(root / "shard-000.sqlite3")
    legacy.initialize(spec, context.calendar, context.universe, context.source_versions)
    batch = _batch_for_security(security, spec)
    legacy.save_batch(spec, batch)

    migrate_legacy_archive(root, spec, context)

    migrated = SQLiteBaoStockDailyShard(root / "shards" / "main-6000.sqlite3")
    assert migrated.snapshot(spec).batches == (batch,)
    assert not (root / "shard-000.sqlite3").exists()
    recovery = tuple((root / "recovery").glob("legacy-*"))
    assert len(recovery) == 1
    assert (recovery[0] / "shard-000.sqlite3").is_file()


def test_checkpoint_inspection_does_not_keep_stale_failure_after_code_completed(tmp_path: Path) -> None:
    coordinator, security, _ = _coordinator(tmp_path)
    spec = coordinator._run.spec
    context = coordinator._run.context
    root = tmp_path / "baostock-daily" / "sessions-1"
    current = SQLiteBaoStockDailyShard(root / "shards" / "main-6000.sqlite3")
    current.initialize(spec, context.calendar, context.universe, context.source_versions)
    current.save_batch(spec, _batch_for_security(security, spec))
    legacy = SQLiteBaoStockDailyShard(root / "shard-000.sqlite3")
    legacy.initialize(spec, context.calendar, context.universe, context.source_versions)
    legacy.record_failure(spec, security.code, "supplier_query_failed")

    status = inspect_baostock_history(tmp_path, sessions=1)

    assert status.completed_codes == 1
    assert status.failed_codes == 0


def test_worker_unavailable_stops_the_run_without_failing_unattempted_codes(tmp_path: Path) -> None:
    coordinator, security, _ = _coordinator(tmp_path)
    coordinator._initialize_shards()

    coordinator._record_unavailable()
    status = coordinator._finish()

    assert status.state == "failed"
    assert status.failure_reasons == ("worker_unavailable",)
    assert status.failed_codes == 0
    assert tuple(coordinator._pending) == (security,)
    assert coordinator._failure_shard(security).checkpoint(coordinator._run.spec).failures == ()


def test_resume_clears_legacy_worker_unavailable_code_failures(tmp_path: Path) -> None:
    coordinator, security, _ = _coordinator(tmp_path)
    coordinator._initialize_shards()
    coordinator._failure_shard(security).record_failure(
        coordinator._run.spec,
        security.code,
        "worker_unavailable",
    )

    resumed, _, _ = _coordinator(tmp_path)
    resumed._initialize_shards()

    assert resumed._failed_codes == set()
    assert tuple(resumed._pending) == (security,)
    assert resumed._failure_shard(security).checkpoint(resumed._run.spec).failures == ()


def test_worker_replacement_preserves_run_failure_and_keeps_the_code_resumable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator, security, _ = _coordinator(tmp_path)
    coordinator._initialize_shards()
    coordinator._pending.clear()

    class _Connection:
        def poll(self) -> bool:
            return False

    class _Process:
        def is_alive(self) -> bool:
            return True

    handle = _WorkerHandle(  # type: ignore[arg-type] -- focused process/connection doubles
        process=_Process(),
        connection=_Connection(),
        shard_path=tmp_path,
        current=security,
        started_at=1.0,
    )
    monkeypatch.setattr(
        "trader.infra.research.baostock_history_runtime._terminate_process",
        lambda _process: None,
    )
    monkeypatch.setattr(coordinator, "_replace", lambda _handle: "supplier_login_failed_blacklisted")

    coordinator._service_worker(handle, now=62.0)
    status = coordinator._finish()

    assert status.state == "failed"
    assert status.failure_reasons == ("supplier_login_failed_blacklisted",)
    assert status.failed_codes == 0
    assert tuple(coordinator._pending) == (security,)


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


def test_repairable_qfq_quality_partition_is_quarantined_at_shard_granularity(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    shards = root / "shards"
    shards.mkdir(parents=True)
    healthy = shards / "main-6000.sqlite3"
    repairable = shards / "main-0019.sqlite3"
    healthy.write_bytes(b"healthy")
    repairable.write_bytes(b"repairable")
    catalog = root / "catalog.sqlite3"
    catalog.write_bytes(b"catalog")
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "catalog_sha256": hashlib.sha256(catalog.read_bytes()).hexdigest(),
                "audit": {"failed_codes": ["001914"]},
                "partitions": [
                    {
                        "relative_path": "shards/main-6000.sqlite3",
                        "database_sha256": hashlib.sha256(healthy.read_bytes()).hexdigest(),
                        "codes": ["600001"],
                    },
                    {
                        "relative_path": "shards/main-0019.sqlite3",
                        "database_sha256": hashlib.sha256(repairable.read_bytes()).hexdigest(),
                        "codes": ["001914", "001965", "001979"],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    _quarantine_corrupt_archive_parts(root)

    assert healthy.is_file()
    assert not repairable.exists()
    quarantine = tuple((root / "quarantine").glob("recovery-*"))
    assert len(quarantine) == 1
    assert (quarantine[0] / repairable.name).is_file()


def test_manifest_path_outside_archive_is_never_moved(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    root.mkdir()
    outside = tmp_path / "outside.sqlite3"
    outside.write_bytes(b"must-stay")
    catalog = root / "catalog.sqlite3"
    catalog.write_bytes(b"catalog")
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "catalog_sha256": hashlib.sha256(catalog.read_bytes()).hexdigest(),
                "partitions": [
                    {
                        "relative_path": "../outside.sqlite3",
                        "database_sha256": hashlib.sha256(outside.read_bytes()).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    _quarantine_corrupt_archive_parts(root)

    assert outside.read_bytes() == b"must-stay"
    assert not (root / "manifest.json").exists()
    quarantine = tuple((root / "quarantine").glob("recovery-*"))
    assert len(quarantine) == 1
    assert not (quarantine[0] / outside.name).exists()


def test_legacy_daily_hash_manifest_is_resealed_without_quarantining_healthy_shard(tmp_path: Path) -> None:
    coordinator, security, _ = _coordinator(tmp_path)
    root = coordinator._run.root
    coordinator._initialize_shards()
    shard = coordinator._failure_shard(security)
    shard.save_batch(coordinator._run.spec, _batch_for_security(security, coordinator._run.spec))
    catalog = root / "catalog.sqlite3"
    catalog.write_bytes(b"legacy-catalog")
    derived = root / "tomorrow-training-dataset.json"
    derived.write_text("{}", encoding="utf-8")
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "catalog_sha256": hashlib.sha256(catalog.read_bytes()).hexdigest(),
                "partitions": [
                    {
                        "relative_path": shard.path.relative_to(root).as_posix(),
                        "database_sha256": legacy_daily_database_sha256(shard.path),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    _quarantine_corrupt_archive_parts(root)

    assert shard.path.is_file()
    assert not (root / "manifest.json").exists()
    recovery = tuple((root / "quarantine").glob("recovery-*"))
    assert len(recovery) == 1
    assert (recovery[0] / "manifest.json").is_file()
    assert (recovery[0] / "catalog.sqlite3").is_file()
    assert (recovery[0] / derived.name).is_file()


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
