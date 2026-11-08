"""Export real problem windows, repair isolated SQLite files, verify immutable parents."""

from __future__ import annotations

from scripts.core.paths import PROJECT_ROOT as TOOL_ROOT

from trader.infra.sqlite_policy import SqliteIoPolicy

import argparse
import tempfile
import platform
import re
import shutil
from time import monotonic
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import cast
from unittest.mock import patch

from trader.download.domain.baostock_daily import BaoStockBoard, BaoStockSecurity
from trader.download.domain.history_price_qualification import NeedsPriceSupplement, QualifyHistoryPairs, QualifyReplacementWindow
from trader.download.domain.history_revision import HistoryRevision
from trader.download.domain.history_sync import HistorySupplierContext, HistorySyncConfiguration
from trader.download.domain.price_review import PriceReviewRequestBudget
from trader.download.domain.published_history import ProjectHistoryCell, PublishedHistoryWindow
from trader.download.infra.baostock_sync_supplier import BaoStockHistorySupplier, BaoStockRequestPolicy
from trader.download.infra.history_control_index import HistoryControlIndex
from trader.download.infra.history_direct_prices import HistoryDirectPrices
from trader.download.infra.history_download_index import HistoryDownloadIndex
from trader.download.infra.history_price_repair import HistoryPriceRepair
from trader.download.infra.history_prices import HistoryPricePartition
from trader.download.infra.published_history_codec import DecodePublishedHistoryCellPayload
from trader.download.infra.history_reference_index import ReadBoundHistoryReference
from trader.download.infra.history_sqlite_activity import HistorySqliteActivity
from trader.bootstrap import ExecuteHistorySync
from trader.download.infra.history_sync_io import HistorySyncResources
from trader.download.infra.download_settings import LoadHistorySyncConfiguration
from trader.download.infra.reference_index import SQLiteReferenceIndex
from trader.infra.process_lock import ProcessLock
from trader.infra.settings.data_paths import DataPaths
from trader.infra.sqlite_connection import SqliteConnection, SqliteConnectionOptions, SqliteSession, SqliteTransaction
from trader.infra.sqlite_codec import DecodeDate

from scripts.core.reporting import emit_report


@dataclass
class _AuditSupplier:
    context: HistorySupplierContext
    windows: tuple[PublishedHistoryWindow, ...]
    baseline: BaoStockHistorySupplier
    seed: bool = True
    calls: int = 0

    def load_context(self, _as_of: date, _sessions: int) -> HistorySupplierContext:
        return self.context

    def fetch_tencent_window(
        self,
        security: BaoStockSecurity,
        dates: tuple[date, ...],
        *,
        request_budget: PriceReviewRequestBudget | None = None,
    ) -> PublishedHistoryWindow:
        if not self.seed:
            raise RuntimeError("audit_must_not_request_tencent")
        window = next(item for item in self.windows if item.code == security.code)
        return PublishedHistoryWindow(security.code, tuple(cell for cell in window.cells if cell.trade_date in dates))

    def fetch_baostock_window(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> PublishedHistoryWindow:
        if self.seed:
            raise RuntimeError("audit_export_only")
        self.calls += 1
        result = self.baseline.fetch_code(security, dates)
        return PublishedHistoryWindow(security.code, tuple(ProjectHistoryCell(cell) for cell in result.batch.cells))


def _require_isolated_work_paths(
    configuration: HistorySyncConfiguration, source_history: Path, source_reference: Path,
) -> None:
    sources = (source_history.resolve(), source_reference.resolve())
    for output in (configuration.history_root, configuration.reference_root):
        resolved = output.resolve()
        if any(resolved.is_relative_to(source) or source.is_relative_to(resolved) for source in sources):
            raise ValueError("audit_work_root_overlaps_source")
        if output.is_symlink():
            raise ValueError("audit_work_root_contains_alias")
        if output.exists():
            for child in output.iterdir():
                if child.is_symlink() or child.is_file() and child.stat().st_nlink > 1:
                    raise ValueError("audit_work_root_contains_alias")


def _audit(
    root: Path, work_root: Path | None = None, *, sqlite_policy: SqliteIoPolicy,
    selected_codes: tuple[str, ...] | None = None,
) -> dict[str, object]:
    control = HistoryControlIndex(root / "catalog.sqlite3", sqlite_policy=sqlite_policy)
    state = control.load_published_state()
    if state is None:
        raise ValueError("audit_requires_published_history")
    gaps = HistoryDownloadIndex(root / "catalog.sqlite3", sqlite_policy=sqlite_policy).gaps_by_code()
    codes = frozenset(gaps) if selected_codes is None else frozenset(selected_codes)
    if selected_codes is not None and (
        not 1 <= len(selected_codes) <= 5 or len(codes) != len(selected_codes)
        or any(re.fullmatch(r"[0-9]{6}", code) is None for code in selected_codes)
    ):
        raise ValueError("repair_audit_codes_invalid")
    if len(codes) > 50:
        raise ValueError("audit_problem_stock_limit_50")
    reference = control.read_universe_reference(state.universe.content_hash)
    universe = tuple(item for item in reference.eligible_universe if item.code in codes)
    if selected_codes is not None and codes != frozenset(item.code for item in universe):
        raise ValueError("repair_audit_code_outside_published_universe")
    snapshot = state.snapshot
    source = HistoryDirectPrices(root, snapshot.sequence, snapshot, HistorySqliteActivity(lambda: None, lambda: False), sqlite_policy=sqlite_policy)
    dates = state.calendar.open_dates
    windows = tuple(PublishedHistoryWindow(item.code, tuple(ProjectHistoryCell(cell) for cell in source.read(item.code, dates))) for item in universe)
    supplements_before = sum(NeedsPriceSupplement(cell) for window in windows for cell in window.cells)
    if not universe:
        return {"status": "passed", "summary": {"problem_stocks": 0, "network_requests": 0}}
    historical = ReadBoundHistoryReference(root, state.source.supplier_contract, sqlite_policy=sqlite_policy)
    subset = replace(reference, official_universe=universe, st_evidence=tuple(item for item in reference.st_evidence if item.code in codes))
    observed = datetime.combine(snapshot.data_cutoff, time(20), ZoneInfo("Asia/Shanghai"))
    with tempfile.TemporaryDirectory(prefix="trader-price-repair-") if work_root is None else nullcontext(str(work_root)) as temporary:
        work = Path(temporary)
        configuration = replace(
            LoadHistorySyncConfiguration(TOOL_ROOT, DataPaths(work / "data")),
            history_root=work / "history",
            reference_root=work / "reference",
            sessions=len(dates),
            minimum_free_bytes=0,
            supplier_retries=0,
        )
        _require_isolated_work_paths(configuration, root, control.reference_path())
        SQLiteReferenceIndex(configuration.reference_root / "company_st.sqlite3", sqlite_policy=sqlite_policy).publish(subset, subset.st_evidence)
        with BaoStockHistorySupplier(configuration, price_policy=BaoStockRequestPolicy(45, 0), vendor_lock=ProcessLock(control.reference_path().parent / "baostock.lock", label="BaoStock")) as baseline:
            from trader.download.domain.baostock_daily import BaoStockCalendar, BaoStockSourceVersions
            context = HistorySupplierContext(BaoStockCalendar(dates), universe, BaoStockSourceVersions("repair-audit", platform.python_version(), ()), tuple(item for item in historical.industry_intervals if item.code in codes), subset.st_evidence, subset.identity)
            supplier = _AuditSupplier(context, windows, baseline)
            resumed = (configuration.history_root / "catalog.sqlite3").exists()
            if not resumed:
                exported = ExecuteHistorySync(
                    configuration,
                    supplier,
                    clock=lambda: observed,
                    sqlite_policy=sqlite_policy,
                )
                if exported.state != "completed":
                    raise RuntimeError(exported.reason or "audit_export_failed")
            exported_state = HistoryControlIndex(
                configuration.history_root / "catalog.sqlite3", sqlite_policy=sqlite_policy,
            ).load_state()
            if exported_state.published_snapshot is None:
                raise RuntimeError("audit_requires_exported_history")
            before = min(exported_state.snapshots, key=lambda item: item.sequence)
            parent = HistoryDirectPrices(configuration.history_root, before.sequence, before, HistorySqliteActivity(lambda: None, lambda: False), sqlite_policy=sqlite_policy)
            if any(
                tuple(ProjectHistoryCell(cell) for cell in parent.read(
                    window.code, tuple(cell.trade_date for cell in window.cells),
                )) != window.cells for window in windows
            ):
                raise RuntimeError("audit_export_windows_mismatch")
            supplier.seed = False
            repaired = ExecuteHistorySync(
                configuration,
                supplier,
                clock=lambda: observed,
                sqlite_policy=sqlite_policy,
                resources=HistorySyncResources(repair_only=True),
            )
            old = HistoryDirectPrices(configuration.history_root, before.sequence, before, HistorySqliteActivity(lambda: None, lambda: False), sqlite_policy=sqlite_policy)
            unchanged = all(tuple(ProjectHistoryCell(cell) for cell in old.read(window.code, tuple(cell.trade_date for cell in window.cells))) == window.cells for window in windows)
            remaining = HistoryDownloadIndex(configuration.history_root / "catalog.sqlite3", sqlite_policy=sqlite_policy).gaps_by_code()
            latest = HistoryControlIndex(
                configuration.history_root / "catalog.sqlite3", sqlite_policy=sqlite_policy,
            ).load_published_state()
            if latest is None:
                raise RuntimeError("audit_repair_publication_missing")
            current = HistoryDirectPrices(
                configuration.history_root, latest.snapshot.sequence, latest.snapshot,
                HistorySqliteActivity(lambda: None, lambda: False), sqlite_policy=sqlite_policy,
            )
            supplements_after = sum(
                NeedsPriceSupplement(ProjectHistoryCell(cell))
                for security in universe for cell in current.read(security.code, dates)
            )
            calls_before_repeat = supplier.calls
            repeated = ExecuteHistorySync(
                configuration, supplier, clock=lambda: observed, sqlite_policy=sqlite_policy,
                resources=HistorySyncResources(repair_only=True),
            ) if not repaired.needs_retry else None
            repeat_requests = supplier.calls - calls_before_repeat
            attempts = []
            with SqliteSession(configuration.history_root / "catalog.sqlite3", sqlite_policy=sqlite_policy) as connection:
                attempts = [dict(zip(("code", "state", "reason", "days"), tuple(row), strict=True)) for row in connection.run("SELECT code,state,reason,COUNT(*) FROM price_repair_attempt GROUP BY code,state,reason ORDER BY code")]
            return {
                "status": "passed" if repaired.state in ("completed", "already_current") and not remaining
                and not supplements_after and unchanged and repeated is not None and not repeated.needs_retry
                and repeat_requests == 0 else "degraded",
                "summary": {"problem_stocks": len(universe), "sessions": len(dates), "gap_days_before": sum(len(gaps.get(code, ())) for code in codes), "gap_days_after": sum(map(len, remaining.values())), "supplier_stock_requests": supplier.calls, "state": repaired.state, "reason": repaired.reason, "old_snapshot_unchanged": unchanged, "repaired_stocks": len(HistoryPriceRepair(configuration.history_root, sqlite_policy=sqlite_policy).baostock_codes()), "attempts": attempts, "active_data_written": False,
                            "supplement_cells_before": supplements_before, "supplement_cells_after": supplements_after,
                            "repeat_supplier_stock_requests": repeat_requests if repeated is not None else None,
                            "repeat_state": repeated.state if repeated is not None else None},
            }


def _source_stats(paths: tuple[Path, ...]) -> tuple[tuple[int, int, int, int] | None, ...]:
    result: list[tuple[int, int, int, int] | None] = []
    for path in paths:
        if path.exists():
            stat = path.stat()
            result.append((stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns))
        else:
            result.append(None)
    return tuple(result)


def _snapshot_options(path: Path) -> SqliteConnectionOptions:
    wal = Path(str(path) + "-wal")
    # A stopped writer's empty/absent WAL needs no shared-memory sidecars.
    # A nonempty WAL must remain visible to SQLite's read-only backup.
    return SqliteConnectionOptions(read_only=True, immutable=not wal.exists() or wal.stat().st_size == 0)


def _profile_repair_year(
    root: Path,
    work: Path,
    year: int,
    code: str,
    *,
    sqlite_policy: SqliteIoPolicy,
) -> dict[str, object]:
    from scripts.download.history.history_publication_measurement import HistoryPublicationMeasurement
    from scripts.infra.database.database_measurement import DatabaseMeasurement

    root = root.resolve()
    if (
        not work.is_absolute() or work.exists() or work.is_symlink()
        or work.resolve().is_relative_to(TOOL_ROOT) or work.resolve().is_relative_to(root)
        or root.is_relative_to(work.resolve()) or not 1990 <= year <= 2100
        or re.fullmatch(r"[0-9]{6}", code) is None
    ):
        raise ValueError("repair_profile_requires_new_external_work_root_and_valid_year_code")
    source = root / f"prices_{year:04d}.sqlite3"
    catalog = root / "catalog.sqlite3"
    protected = (catalog, Path(str(catalog) + "-wal"), source, Path(str(source) + "-wal"))
    before = _source_stats(protected)
    with SqliteSession(catalog, _snapshot_options(catalog), sqlite_policy=sqlite_policy) as connection:
        published = connection.run("SELECT MAX(sync_sequence) FROM published_snapshot").require_one()[0]
        rows = connection.run(
            "SELECT trade_date,sync_sequence,board,source,payload FROM price_replacement WHERE code=? ORDER BY trade_date LIMIT 2001",
            (code,),
        ).fetchall()
    if not rows or len(rows) > 2000 or any(tuple(row[1:4]) != tuple(rows[0][1:4]) for row in rows):
        raise ValueError("repair_profile_requires_one_complete_journal_window")
    sequence = rows[0][1]
    if (published is not None and published >= sequence) or rows[0][3] not in ("tencent", "baostock"):
        raise ValueError("repair_profile_requires_unpublished_sequence")
    dates = tuple(DecodeDate(row[0]) for row in rows)
    window = PublishedHistoryWindow(code, tuple(
        DecodePublishedHistoryCellPayload(code, day.isoformat(), row[4])
        for day, row in zip(dates, rows, strict=True)
    ))
    QualifyReplacementWindow(dates, PublishedHistoryWindow(code, ()), window)
    revisions = tuple(
        HistoryRevision(sequence, cast(BaoStockBoard, rows[0][2]), cell, None, None, None)
        for cell in QualifyHistoryPairs(code, dates, window) if cell.trade_date.year == year
    )
    if not revisions:
        return {"status": "empty", "summary": {"reason": "candidate_year_empty", "network_requests": 0}}
    if not source.is_file() or source.is_symlink() or source.stat().st_size > 700 * 1024**2:
        raise ValueError("repair_profile_requires_annual_file_below_700_mib")
    if shutil.disk_usage(work.parent).free < source.stat().st_size * 2 + 64 * 1024**2:
        raise ValueError("repair_profile_disk_space_insufficient")
    work.mkdir()
    target = work / source.name
    measurement = HistoryPublicationMeasurement(profiler=False)
    changed = 0
    changed_operation = HistoryPricePartition.changed_revisions

    def changed_revisions(partition: HistoryPricePartition, values: tuple[HistoryRevision, ...]) -> tuple[HistoryRevision, ...]:
        nonlocal changed
        with measurement.phase("repair_changed_revisions", source.name):
            result = tuple(changed_operation(partition, values))
            changed = len(result)
            return result

    try:
        with measurement.phase("source_backup", source.name):
            with (
                SqliteSession(source, _snapshot_options(source), sqlite_policy=sqlite_policy) as original,
                SqliteSession(target, sqlite_policy=sqlite_policy) as duplicate,
            ):
                original.backup_to(duplicate, deadline=monotonic() + 180.0, monotonic=monotonic)
        with DatabaseMeasurement(work, sample_wal=False) as database:
            with (
                patch.object(HistoryPricePartition, "changed_revisions", changed_revisions),
                patch.object(HistoryPricePartition, "_save_batch", measurement._timed("repair_write", HistoryPricePartition._save_batch)),
                patch.object(SqliteTransaction, "commit", measurement._timed("repair_commit", SqliteTransaction.commit)),
                patch.object(SqliteConnection, "close", measurement._timed("repair_close", SqliteConnection.close)),
                measurement.phase("repair_replace_total", source.name),
            ):
                HistoryPricePartition(target, year, 1, sqlite_policy=sqlite_policy).replace_unpublished_code(revisions, sequence)
    finally:
        if _source_stats(protected) != before:
            raise RuntimeError("repair_profile_source_changed")
    return {
        "status": "passed" if changed else "empty",
        "summary": {
            "code": code, "year": year, "sequence": sequence, "candidate_window_rows": len(dates),
            "candidate_year_rows": len(revisions), "changed_rows": changed,
            "source_database_bytes": source.stat().st_size, "network_requests": 0,
            "active_data_written": False, "source_stat_unchanged": True, "performance_acceptance": False,
            "database_measurement": database.result, "publication_measurement": measurement.report(),
        },
    }


def Main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history-root", type=Path, required=True)
    parser.add_argument("--runtime-config", type=Path, default=TOOL_ROOT / "config/runtime.json")
    parser.add_argument("--work-root", type=Path)
    parser.add_argument("--repair-profile-year", type=int)
    parser.add_argument("--repair-profile-code")
    parser.add_argument("--codes", nargs="+")
    args = parser.parse_args()
    if args.codes is not None and (args.repair_profile_year is not None or args.repair_profile_code is not None):
        parser.error("--codes cannot be combined with --repair-profile-year or --repair-profile-code")
    from trader.bootstrap import BuildSqlitePolicy
    from trader.infra.settings import LoadRuntimeSettings
    sqlite_policy = BuildSqlitePolicy(LoadRuntimeSettings(args.runtime_config))
    if args.repair_profile_year is not None or args.repair_profile_code is not None:
        if args.repair_profile_year is None or args.repair_profile_code is None or args.work_root is None:
            parser.error("repair profile requires year, code and a new external work root")
        emit_report({"schema_version": "price-repair-audit", **_profile_repair_year(
            args.history_root, args.work_root, args.repair_profile_year, args.repair_profile_code,
            sqlite_policy=sqlite_policy,
        )})
        return 0
    if args.work_root is not None:
        project = TOOL_ROOT
        if not args.work_root.is_absolute() or args.work_root.resolve().is_relative_to(project):
            parser.error("work root must be an absolute path outside the repository")
        work = args.work_root.resolve()
        source = args.history_root.resolve()
        if work.is_relative_to(source) or source.is_relative_to(work):
            parser.error("work root must not overlap source history")
    if args.codes is not None and args.work_root is None:
        parser.error("explicit repair codes require an external work root")
    emit_report({"schema_version": "price-repair-audit", **_audit(
        args.history_root, args.work_root, sqlite_policy=sqlite_policy,
        selected_codes=tuple(args.codes) if args.codes is not None else None,
    )})
    return 0
