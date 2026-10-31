"""Export real problem windows, repair isolated SQLite files, verify immutable parents."""

from __future__ import annotations

from trader.infra.sqlite_policy import SqliteIoPolicy

import argparse
import tempfile
import platform
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.download.domain.history_sync import HistorySupplierContext
from trader.download.domain.published_history import ProjectHistoryCell, PublishedHistoryWindow
from trader.download.infra.baostock_sync_supplier import BaoStockHistorySupplier, BaoStockRequestPolicy
from trader.download.infra.history_control_index import HistoryControlIndex
from trader.download.infra.history_direct_prices import HistoryDirectPrices
from trader.download.infra.history_download_index import HistoryDownloadIndex
from trader.download.infra.history_price_repair import HistoryPriceRepair
from trader.download.infra.history_reference_index import ReadBoundHistoryReference
from trader.download.infra.history_sqlite_activity import HistorySqliteActivity
from trader.download.infra.history_sync_runner import RunHistorySync
from trader.download.infra.download_settings import LoadHistorySyncConfiguration
from trader.download.infra.reference_index import SQLiteReferenceIndex
from trader.infra.process_lock import ProcessLock
from trader.infra.settings.data_paths import DataPaths
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction

from .reporting import emit_report


@dataclass
class _AuditSupplier:
    context: HistorySupplierContext
    windows: tuple[PublishedHistoryWindow, ...]
    baseline: BaoStockHistorySupplier
    seed: bool = True
    calls: int = 0

    def load_context(self, _as_of: date, _sessions: int) -> HistorySupplierContext:
        return self.context

    def fetch_tencent_window(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> PublishedHistoryWindow:
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


def _audit(root: Path, work_root: Path | None = None, *, sqlite_policy: SqliteIoPolicy) -> dict[str, object]:
    control = HistoryControlIndex(root / "catalog.sqlite3", sqlite_policy=sqlite_policy)
    state = control.load_published_state()
    if state is None:
        raise ValueError("audit_requires_published_history")
    gaps = HistoryDownloadIndex(root / "catalog.sqlite3", sqlite_policy=sqlite_policy).gaps_by_code()
    codes = frozenset(gaps)
    if len(codes) > 50:
        raise ValueError("audit_problem_stock_limit_50")
    reference = control.read_universe_reference(state.universe.content_hash)
    universe = tuple(item for item in reference.eligible_universe if item.code in codes)
    snapshot = state.snapshot
    source = HistoryDirectPrices(root, snapshot.sequence, snapshot, HistorySqliteActivity(lambda: None, lambda: False), sqlite_policy=sqlite_policy)
    dates = state.calendar.open_dates
    windows = tuple(PublishedHistoryWindow(item.code, tuple(ProjectHistoryCell(cell) for cell in source.read(item.code, dates))) for item in universe)
    if not universe:
        return {"status": "passed", "summary": {"problem_stocks": 0, "network_requests": 0}}
    historical = ReadBoundHistoryReference(root, state.source.supplier_contract, sqlite_policy=sqlite_policy)
    subset = replace(reference, official_universe=universe, st_evidence=tuple(item for item in reference.st_evidence if item.code in codes))
    observed = datetime.combine(snapshot.data_cutoff, time(20), ZoneInfo("Asia/Shanghai"))
    with tempfile.TemporaryDirectory(prefix="trader-price-repair-") if work_root is None else nullcontext(str(work_root)) as temporary:
        work = Path(temporary)
        configuration = replace(
            LoadHistorySyncConfiguration(Path(__file__).resolve().parents[2], DataPaths(work / "data")),
            history_root=work / "history",
            reference_root=work / "reference",
            sessions=len(dates),
            minimum_free_bytes=0,
            supplier_retries=0,
        )
        SQLiteReferenceIndex(configuration.reference_root / "company_st.sqlite3", sqlite_policy=sqlite_policy).publish(subset, subset.st_evidence)
        with BaoStockHistorySupplier(configuration, price_policy=BaoStockRequestPolicy(45, 0), vendor_lock=ProcessLock(control.reference_path().parent / "baostock.lock", label="BaoStock")) as baseline:
            from trader.download.domain.baostock_daily import BaoStockCalendar, BaoStockSourceVersions
            context = HistorySupplierContext(BaoStockCalendar(dates), universe, BaoStockSourceVersions("repair-audit", platform.python_version(), ()), tuple(item for item in historical.industry_intervals if item.code in codes), subset.st_evidence, subset.identity)
            supplier = _AuditSupplier(context, windows, baseline)
            resumed = (configuration.history_root / "catalog.sqlite3").exists()
            if not resumed:
                exported = RunHistorySync(configuration, supplier, clock=lambda: observed, sqlite_policy=sqlite_policy)
                if exported.state != "completed":
                    raise RuntimeError(exported.reason or "audit_export_failed")
            before = HistoryControlIndex(configuration.history_root / "catalog.sqlite3", sqlite_policy=sqlite_policy).load_published_state()
            assert before is not None
            # Rehearse the additive legacy metadata migration on an isolated v2
            # catalog containing the real gap/price observations.
            with SqliteSession(configuration.history_root / "catalog.sqlite3", sqlite_policy=sqlite_policy) as connection, SqliteTransaction(connection, immediate=True):
                if not resumed:
                    for table in ("price_repair_attempt", "price_source", "price_replacement", "price_tencent_check"):
                        connection.run(f"DROP TABLE {table}")
                else:
                    connection.run("UPDATE download_tasks SET tencent_done=1")
            supplier.seed = False
            repaired = RunHistorySync(configuration, supplier, clock=lambda: observed, repair_only=True, sqlite_policy=sqlite_policy)
            old = HistoryDirectPrices(configuration.history_root, before.snapshot.sequence, before.snapshot, HistorySqliteActivity(lambda: None, lambda: False), sqlite_policy=sqlite_policy)
            unchanged = all(tuple(ProjectHistoryCell(cell) for cell in old.read(window.code, tuple(cell.trade_date for cell in window.cells))) == window.cells for window in windows)
            remaining = HistoryDownloadIndex(configuration.history_root / "catalog.sqlite3", sqlite_policy=sqlite_policy).gaps_by_code()
            attempts = []
            with SqliteSession(configuration.history_root / "catalog.sqlite3", sqlite_policy=sqlite_policy) as connection:
                attempts = [dict(zip(("code", "state", "reason", "days"), tuple(row), strict=True)) for row in connection.run("SELECT code,state,reason,COUNT(*) FROM price_repair_attempt GROUP BY code,state,reason ORDER BY code")]
            return {
                "status": "passed" if repaired.state in ("completed", "already_current") and not remaining and unchanged else "degraded",
                "summary": {"problem_stocks": len(universe), "sessions": len(dates), "gap_days_before": sum(map(len, gaps.values())), "gap_days_after": sum(map(len, remaining.values())), "supplier_stock_requests": supplier.calls, "state": repaired.state, "reason": repaired.reason, "old_snapshot_unchanged": unchanged, "repaired_stocks": len(HistoryPriceRepair(configuration.history_root, sqlite_policy=sqlite_policy).baostock_codes()), "attempts": attempts, "active_data_written": False},
            }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history-root", type=Path, required=True)
    parser.add_argument("--work-root", type=Path)
    args = parser.parse_args()
    from trader.bootstrap import BuildSqlitePolicy
    from trader.infra.settings import LoadRuntimeSettings
    sqlite_policy = BuildSqlitePolicy(LoadRuntimeSettings(Path(__file__).resolve().parents[2] / "config/runtime.json"))
    if args.work_root is not None:
        project = Path(__file__).resolve().parents[2]
        if not args.work_root.is_absolute() or args.work_root.resolve().is_relative_to(project):
            parser.error("work root must be an absolute path outside the repository")
    emit_report({"schema_version": "price-repair-audit", **_audit(args.history_root, args.work_root, sqlite_policy=sqlite_policy)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
