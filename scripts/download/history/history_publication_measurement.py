"""Scoped publication profiling in an isolated diagnostic worker; never installed in the product."""

from __future__ import annotations

import cProfile
import resource
import shutil
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock, local
from types import TracebackType
from typing import Literal, ParamSpec, Protocol, TypeVar
from unittest.mock import patch

from trader.download.domain.history_control import HistorySnapshotPartition
from trader.download.infra import history_control_index, history_direct_prices, history_prices, history_sync_runner
from trader.download.infra.history_control_index import HistoryControlIndex
from trader.download.infra.history_direct_prices import HistoryDirectPrices
from trader.download.infra.history_prices import HistoryPricePartition
from trader.infra.sqlite_policy import SqliteIoPolicy
from trader.infra.sqlite_publication import (
    PreparedSqlitePublication,
    PrepareSqliteFilePublication,
    PublishPreparedSqliteFile,
    PublishSqliteFile,
    SqlitePublicationFingerprint,
    VerifiedSqlitePublication,
    VerifySqliteFile,
)

from scripts.infra.database.database_measurement import DatabaseMeasurement

_Parameters = ParamSpec("_Parameters")
_Value = TypeVar("_Value")
HistoryPublicationCandidate = Literal["baseline", "exact-month-ranges", "fingerprint-first", "combined"]


class _SealMonths(Protocol):
    def __call__(
        self,
        partition: HistoryPricePartition,
        calendar_months: tuple[int, ...],
        sequence: int | None = None,
        *,
        exact_month_ranges: bool = False,
        fingerprint_before_statistics: bool = False,
    ) -> tuple[HistorySnapshotPartition, ...]: ...


@dataclass(frozen=True)
class _ProcessIo:
    logical_read: int
    logical_write: int
    physical_read: int
    physical_write: int


class _ThreadPhases(local):
    def __init__(self) -> None:
        self.parents: list[int] = []


def _process_io() -> _ProcessIo | None:
    try:
        fields = {
            key: int(value)
            for key, value in (line.split(":") for line in Path("/proc/self/io").read_text().splitlines())
        }
        return _ProcessIo(fields["rchar"], fields["wchar"], fields["read_bytes"], fields["write_bytes"])
    except (OSError, KeyError, ValueError):
        return None


class HistoryPublicationMeasurement:
    """Observe public history operations; nested inclusive phases must not be added together."""

    def __init__(self, *, profiler: bool = True) -> None:
        self._stack = ExitStack()
        self._thread = _ThreadPhases()
        self._lock = Lock()
        self._records: list[dict[str, object]] = []
        self._profiler = profiler

    @contextmanager
    def phase(self, name: str, label: str, *, profile: bool = True) -> Iterator[dict[str, object]]:
        parents = self._thread.parents
        with self._lock:
            record: dict[str, object] = {
                "id": len(self._records), "parent_id": parents[-1] if parents else None,
                "phase": name, "file": label, "completed": False,
            }
            parents.append(len(self._records))
            self._records.append(record)
        before = _process_io()
        wall, cpu = time.perf_counter(), time.process_time()
        profiler = cProfile.Profile() if self._profiler and profile and sys.getprofile() is None else None
        record["profile_owned_here"] = profiler is not None
        if profiler is not None:
            profiler.enable()
        try:
            yield record
            record["completed"] = True
        finally:
            if profiler is not None:
                profiler.disable()
            elapsed, consumed = time.perf_counter() - wall, time.process_time() - cpu
            after = _process_io()
            parents.pop()
            record.update({
                "wall_seconds": elapsed, "cpu_seconds": consumed,
                "logical_read_bytes": after.logical_read - before.logical_read if before and after else None,
                "logical_write_bytes": after.logical_write - before.logical_write if before and after else None,
                "physical_read_bytes": after.physical_read - before.physical_read if before and after else None,
                "physical_write_bytes": after.physical_write - before.physical_write if before and after else None,
                "process_peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                * (1 if sys.platform == "darwin" else 1024),
            })
            if profiler is not None:
                record["primitive_seconds"] = _primitive_seconds(profiler)
                record["operation_calls"] = {
                    name: sum(
                        entry.callcount for entry in profiler.getstats()
                        if not isinstance(entry.code, str) and entry.code.co_name == name
                    )
                    for name in (
                        "_digest", "PublishSqliteFile", "PrepareSqliteFilePublication", "PublishPreparedSqliteFile",
                        "VerifySqliteFile", "_read_connection", "_require_schema",
                    )
                }

    def __enter__(self) -> HistoryPublicationMeasurement:
        seal: _SealMonths = HistoryPricePartition.seal_months

        def measured_seal(
            partition: HistoryPricePartition,
            calendar_months: tuple[int, ...],
            sequence: int | None = None,
            *,
            exact_month_ranges: bool = False,
            fingerprint_before_statistics: bool = False,
        ) -> tuple[HistorySnapshotPartition, ...]:
            with self.phase("annual_seal", "annual") as record:
                record["months"] = calendar_months
                record["sequence"] = sequence
                record["exact_month_ranges"] = exact_month_ranges
                record["fingerprint_before_statistics"] = fingerprint_before_statistics
                values = seal(
                    partition, calendar_months, sequence,
                    exact_month_ranges=exact_month_ranges,
                    fingerprint_before_statistics=fingerprint_before_statistics,
                )
                record["file"] = values[0].relative_path
                record["row_counts"] = tuple(item.row_count for item in values)
                return values

        def measured_publish(
            path: Path,
            *,
            sqlite_policy: SqliteIoPolicy,
            check: Callable[[], None] | None = None,
        ) -> SqlitePublicationFingerprint:
            with self.phase("file_seal", path.name) as record:
                fingerprint = PublishSqliteFile(path, sqlite_policy=sqlite_policy, check=check)
                record.update({
                    "database_bytes": fingerprint.database_size, "database_sha256": fingerprint.database_sha256,
                    "wal_bytes": fingerprint.wal_size, "wal_sha256": fingerprint.wal_sha256,
                })
                return fingerprint

        def measured_prepare(
            path: Path,
            *,
            sqlite_policy: SqliteIoPolicy,
            check: Callable[[], None] | None = None,
        ) -> PreparedSqlitePublication:
            with self.phase("file_fingerprint", path.name) as record:
                prepared = PrepareSqliteFilePublication(path, sqlite_policy=sqlite_policy, check=check)
                record["database_bytes"] = prepared.fingerprint.database_size
                record["wal_bytes"] = prepared.fingerprint.wal_size
                return prepared

        def measured_finalize(
            prepared: PreparedSqlitePublication,
            *,
            check: Callable[[], None] | None = None,
        ) -> SqlitePublicationFingerprint:
            with self.phase("file_receipt", prepared.path.name) as record:
                fingerprint = PublishPreparedSqliteFile(prepared, check=check)
                record.update({
                    "database_bytes": fingerprint.database_size, "database_sha256": fingerprint.database_sha256,
                    "wal_bytes": fingerprint.wal_size, "wal_sha256": fingerprint.wal_sha256,
                })
                return fingerprint

        try:
            self._stack.enter_context(patch.object(history_prices, "PrepareSqliteFilePublication", measured_prepare))
            self._stack.enter_context(patch.object(history_prices, "PublishPreparedSqliteFile", measured_finalize))
            for module in (history_control_index, history_direct_prices, history_prices, history_sync_runner):
                self._stack.enter_context(patch.object(module, "PublishSqliteFile", measured_publish))
            for owner, name, replacement in (
                (HistoryPricePartition, "seal_months", measured_seal),
                (HistoryPricePartition, "has_observations_after", self._timed("change_probe", HistoryPricePartition.has_observations_after)),
                (HistoryDirectPrices, "close_pipeline", self._timed("writer_close", HistoryDirectPrices.close_pipeline)),
                (HistoryDirectPrices, "publishable_partitions", self._timed("partitions_total", HistoryDirectPrices.publishable_partitions, profile=False)),
                (HistoryControlIndex, "publish_file", self._timed("catalog_seal", HistoryControlIndex.publish_file)),
            ):
                self._stack.enter_context(patch.object(owner, name, replacement))
        except BaseException:
            self._stack.close()
            raise
        return self

    def _timed(
        self,
        phase: str,
        operation: Callable[_Parameters, _Value],
        *,
        profile: bool = True,
    ) -> Callable[_Parameters, _Value]:
        def timed(*args: _Parameters.args, **kwargs: _Parameters.kwargs) -> _Value:
            with self.phase(phase, "catalog.sqlite3" if phase == "catalog_seal" else "history", profile=profile):
                return operation(*args, **kwargs)

        return timed

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self._stack.close()

    def report(self) -> dict[str, object]:
        return {
            "profiling_enabled": self._profiler,
            "performance_acceptance": False,
            "timing": "inclusive; parent phases include children; profiler overhead is included"
            if self._profiler else "inclusive; parent phases include children; cProfile is disabled",
            "cache_state": "uncontrolled; use physical_read_bytes instead of assuming cold HDD",
            "io_scope": "process-wide; concurrent worker work and measurement reads may be included",
            "phases": self._records,
        }


def _primitive_seconds(profiler: cProfile.Profile) -> dict[str, float]:
    # Primitive self time excludes callers, so execute/fetch are counted without double-counting SQL wrappers.
    totals = dict.fromkeys(("sqlite_execute_fetch", "sqlite_close", "file_read", "sha256_update", "fsync"), 0.0)
    for entry in profiler.getstats():
        name = entry.code
        if not isinstance(name, str):
            continue
        category = None
        if "sqlite3." in name and any(word in name for word in ("execute", "fetch")):
            category = "sqlite_execute_fetch"
        elif "sqlite3." in name and "close" in name:
            category = "sqlite_close"
        elif "_io." in name and any(word in name for word in ("'read'", "'readinto'", "'read1'")):
            category = "file_read"
        elif "HASH" in name and "update" in name:
            category = "sha256_update"
        elif name in {"<built-in method posix.fsync>", "<built-in method nt.fsync>"}:
            category = "fsync"
        if category is not None:
            totals[category] += entry.inlinetime
    return totals


def ProfilePublishedHistory(
    root: Path,
    *,
    sqlite_policy: SqliteIoPolicy,
    candidate: HistoryPublicationCandidate = "baseline",
    profiler: bool = True,
) -> dict[str, object]:
    """Re-seal byte copies of a fixed published input, one annual file at a time; no supplier calls."""
    if candidate not in {"baseline", "exact-month-ranges", "fingerprint-first", "combined"}:
        raise ValueError("history_publication_candidate_invalid")
    root = root.resolve()
    catalog = root / "catalog.sqlite3"
    started = time.perf_counter()
    with HistoryPublicationMeasurement(profiler=profiler) as publication, DatabaseMeasurement(root, sample_wal=False) as database:
        with publication.phase("source_verification", catalog.name):
            catalog_verified = VerifySqliteFile(catalog, sqlite_policy=sqlite_policy)
        if catalog_verified.wal is not None:
            raise ValueError("history_publication_profile_requires_closed_writers")
        with publication.phase("published_metadata", catalog.name):
            state = HistoryControlIndex(catalog, sqlite_policy=sqlite_policy).load_published_state()
        if state is None:
            raise ValueError("history_publication_profile_requires_published_snapshot")
        snapshot = state.snapshot
        groups: dict[str, list[HistorySnapshotPartition]] = {}
        for reference in snapshot.partitions:
            groups.setdefault(reference.relative_path, []).append(reference)
        files: list[dict[str, object]] = []
        inputs: dict[Path, VerifiedSqlitePublication] = {catalog: catalog_verified}
        for name, references in sorted(groups.items()):
            source = root / name
            with publication.phase("source_verification", name):
                verified = VerifySqliteFile(source, sqlite_policy=sqlite_policy)
            inputs[source] = verified
            if verified.wal is not None:
                raise ValueError("history_publication_profile_requires_closed_writers")
            with TemporaryDirectory(prefix="trader-history-publication-") as directory:
                target = Path(directory) / name
                with publication.phase("copy_preparation", name, profile=False):
                    shutil.copyfile(source, target)
                with publication.phase("source_stability", name):
                    if VerifySqliteFile(source, sqlite_policy=sqlite_policy, previous=verified) != verified:
                        raise RuntimeError("history_publication_profile_source_changed")
                ordered = tuple(sorted(references, key=lambda item: item.calendar_month))
                sealed = HistoryPricePartition(
                    target, ordered[0].calendar_year, ordered[0].calendar_month, sqlite_policy=sqlite_policy,
                ).seal_months(
                    tuple(item.calendar_month for item in ordered), snapshot.sequence,
                    exact_month_ranges=candidate in {"exact-month-ranges", "combined"},
                    fingerprint_before_statistics=candidate in {"fingerprint-first", "combined"},
                )
                with publication.phase("copy_verification", name):
                    copy_verified = VerifySqliteFile(target, sqlite_policy=sqlite_policy)
                equal = (
                    tuple(item.row_count for item in sealed) == tuple(item.row_count for item in ordered)
                    and copy_verified.fingerprint == verified.fingerprint
                )
                files.append({"file": name, "bytes": verified.fingerprint.database_size, "counts_and_bytes_equal": equal})
        with TemporaryDirectory(prefix="trader-history-publication-") as directory:
            target = Path(directory) / catalog.name
            with publication.phase("copy_preparation", catalog.name, profile=False):
                shutil.copyfile(catalog, target)
            HistoryControlIndex(target, sqlite_policy=sqlite_policy).publish_file()
            with publication.phase("copy_verification", catalog.name):
                copied = VerifySqliteFile(target, sqlite_policy=sqlite_policy)
            files.append({
                "file": catalog.name, "bytes": catalog_verified.fingerprint.database_size,
                "counts_and_bytes_equal": copied.fingerprint == catalog_verified.fingerprint,
            })
        with publication.phase("final_source_stability", "history"):
            for source, previous in inputs.items():
                if VerifySqliteFile(source, sqlite_policy=sqlite_policy, previous=previous) != previous:
                    raise RuntimeError("history_publication_profile_source_changed")
    return {
        "schema_version": "history-publication-profile",
        "status": "passed" if all(item["counts_and_bytes_equal"] for item in files) else "failed",
        "summary": {
            "fixture": False, "workload": "republication_of_sealed_snapshot_on_isolated_copies",
            "candidate": candidate,
            "snapshot_hash": snapshot.content_hash, "sequence": snapshot.sequence,
            "seconds": time.perf_counter() - started, "database": database.result,
            "source_and_preparation_included": True, "copy_cache": "recent copy may warm source and target pages",
            "files": files, "publication": publication.report(),
        },
    }


__all__ = [
    "HistoryPublicationCandidate",
    "HistoryPublicationMeasurement",
    "ProfilePublishedHistory",
]
