"""Repeatable offline first-build and incremental-write measurements outside active data."""

from __future__ import annotations

import argparse
import math
import resource
import time
from _thread import LockType
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from datetime import time as daytime
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from zoneinfo import ZoneInfo

from trader.download.domain.baostock_daily import (
    BaoStockCalendar,
    BaoStockCodeBatch,
    BaoStockCodeDownload,
    BaoStockDailyCell,
    BaoStockDailyFact,
    BaoStockDailySide,
    BaoStockIndustryInterval,
    BaoStockSecurity,
    BaoStockSourceVersions,
)
from trader.download.domain.history_reference import HistoryStEvidence
from trader.download.domain.history_sync import (
    HistoryPipelineProgress,
    HistorySupplierContext,
    HistorySyncConfiguration,
    HistorySyncProgress,
)
from trader.download.domain.prepared_reference import PreparedReference
from trader.download.domain.published_history import ProjectHistoryCell, PublishedHistoryWindow
from trader.download.infra.download_settings import LoadHistorySyncConfiguration
from trader.download.infra.history_sync_runner import RunHistorySync
from trader.download.infra.history_write_tuning import (
    HistoryWriteTuning,
)
from trader.download.infra.published_history_snapshot import SQLitePublishedHistorySnapshot
from trader.download.infra.reference_index import SQLiteReferenceIndex
from trader.infra.settings.data_paths import DataPaths
from trader.infra.sqlite_connection import (
    SqliteConnectionOptions,
    SqliteSession,
    SqliteTransaction,
)
from trader.infra.storage_probe import StorageProbe
from trader.infra.storage_types import StorageKind

from .database_measurement import DatabaseMeasurement
from .reporting import emit_report


@dataclass
class HistoryFixtureSupplier:
    days: tuple[date, ...]
    size: int
    requests: list[int] = field(default_factory=list)
    varying_prices: bool = field(default=False, kw_only=True)
    reference_id: str | None = field(default=None, kw_only=True)

    def load_context(self, as_of: date, sessions: int) -> HistorySupplierContext:
        universe = tuple(
            BaoStockSecurity(f"{600000 + i}", "fixture", "main", date(1990, 1, 1), None, "fixture")
            for i in range(self.size)
        )
        return HistorySupplierContext(
            BaoStockCalendar(self.days[-sessions:]),
            universe,
            BaoStockSourceVersions("fixture", "3.10", ()),
            tuple(BaoStockIndustryInterval(item.code, self.days[0], None, "fixture", "fixture") for item in universe)
            if self.varying_prices
            else (),
            tuple(HistoryStEvidence(item.code, as_of, "clear") for item in universe),
            self.reference_id,
        )

    def publish_reference(self, path: Path, as_of: date, sessions: int) -> None:
        context = self.load_context(as_of, sessions)
        reference = PreparedReference(
            context.universe,
            context.st_evidence,
            datetime.combine(as_of, daytime(20, 30), ZoneInfo("Asia/Shanghai")),
            self.days,
        )
        SQLiteReferenceIndex(path).publish(reference, reference.st_evidence)
        self.reference_id = reference.identity

    def fetch_tencent_window(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> PublishedHistoryWindow:
        self.requests.append(len(dates))
        cells = self.fetch_baostock_prices(security, dates).batch.cells
        return PublishedHistoryWindow(security.code, tuple(ProjectHistoryCell(cell) for cell in cells))

    def fetch_baostock_prices(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> BaoStockCodeDownload:
        cells = []
        for day in dates:
            if self.varying_prices:
                cells.append(self._varying_cell(security.code, day))
                continue
            raw = BaoStockDailySide(
                security.code, day, "unadjusted", 10.0, 10.0, 10.0, 10.0, 100.0, 1000.0, 10.0, 0.0, 0.0, "trading"
            )
            qfq = BaoStockDailySide(
                security.code, day, "qfq", 9.0, 9.0, 9.0, 9.0, 100.0, 1000.0, None, None, None, "trading"
            )
            cells.append(BaoStockDailyCell(security.code, day, "complete", raw, qfq))
        return BaoStockCodeDownload(
            BaoStockCodeBatch(security.code, tuple(cells)),
            tuple(BaoStockDailyFact(security.code, day, False) for day in dates),
        )

    def fetch_baostock_window(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> PublishedHistoryWindow:
        result = self.fetch_baostock_prices(security, dates)
        return PublishedHistoryWindow(security.code, tuple(ProjectHistoryCell(cell) for cell in result.batch.cells))

    def _varying_cell(self, code: str, day: date) -> BaoStockDailyCell:
        stock = int(code)
        offset = (day - date(2018, 1, 1)).days
        base = 10.0 + (stock % 1000) * 0.03
        preclose, close = (
            base
            * math.exp(
                0.08 * math.sin(position / 503 + stock * 0.017)
                + 0.04 * math.sin(position / 97 + stock * 0.11)
                + 0.02 * math.sin(position / 13 + stock * 0.37)
                + 0.006 * math.sin(position / 3 + stock * 0.19)
            )
            for position in (offset - 1, offset)
        )
        opening = preclose * (1.0 + 0.002 * math.sin(offset / 7 + stock * 0.13))
        high = max(opening, close) * 1.003
        low = min(opening, close) * 0.997
        volume = 100_000.0 * (1.0 + (stock % 11) * 0.04 + 0.15 * math.sin(offset / 11 + stock * 0.23))
        raw = BaoStockDailySide(
            code,
            day,
            "unadjusted",
            opening,
            high,
            low,
            close,
            volume,
            volume * (opening + close) / 2,
            preclose,
            (close / preclose - 1.0) * 100,
            0.5 + 0.2 * math.cos(offset / 17 + stock * 0.29),
            "trading",
        )
        qfq = replace(
            raw,
            adjustment="qfq",
            open_price=opening * 0.9,
            high_price=high * 0.9,
            low_price=low * 0.9,
            close_price=close * 0.9,
            preclose=None,
            pct_change=None,
            turnover=None,
        )
        return BaoStockDailyCell(code, day, "complete", raw, qfq)


@dataclass
class _StageTimes:
    starts: dict[str, float] = field(default_factory=dict)
    ends: dict[str, float] = field(default_factory=dict)
    lock: LockType = field(default_factory=Lock)
    cancel_after: int | None = None
    cancelled: bool = False
    pipeline: HistoryPipelineProgress | None = None

    def publish(self, progress: HistorySyncProgress) -> None:
        if progress.stage not in {"loading_context", "tencent_history", "sealing_partitions", "publishing_snapshot"}:
            return
        with self.lock:
            now = time.monotonic()
            if progress.pipeline is not None:
                self.pipeline = progress.pipeline
            if (
                self.cancel_after is not None
                and progress.stage == "tencent_history"
                and progress.state == "completed"
                and progress.completed_units >= self.cancel_after
            ):
                self.cancelled = True
            if progress.state in {"started", "waiting"}:
                self.starts.setdefault(progress.stage, now)
            if progress.state == "completed":
                self.ends[progress.stage] = now

    def summary(self) -> dict[str, float]:
        result = {
            stage: round(self.ends[stage] - started, 3) for stage, started in self.starts.items() if stage in self.ends
        }
        if "sealing_partitions" in self.starts and "publishing_snapshot" in self.starts:
            result["sealing_partitions"] = round(
                self.starts["publishing_snapshot"] - self.starts["sealing_partitions"], 3
            )
        if self.pipeline is not None:
            result["writer_cumulative"] = round(self.pipeline.write_seconds, 3)
            result["supplier_attempts_cumulative"] = round(self.pipeline.download_seconds, 3)
            result["resume_reads_cumulative"] = round(self.pipeline.read_seconds, 3)
            result["validation_cumulative"] = round(self.pipeline.validation_seconds, 3)
        return result


def _write_bytes() -> int:
    path = Path("/proc/self/io")
    if not path.exists():
        return 0
    return next(int(line.split(":")[1]) for line in path.read_text().splitlines() if line.startswith("write_bytes:"))


def _clock(observed: datetime) -> Callable[[], datetime]:
    return lambda: observed


def _measure_case(
    name: str,
    config: HistorySyncConfiguration,
    supplier: HistoryFixtureSupplier,
    tuning: HistoryWriteTuning | None,
    storage_kind: StorageKind | None,
) -> dict[str, object]:
    supplier.requests.clear()
    now = datetime.combine(supplier.days[-1], daytime(20, 30), ZoneInfo("Asia/Shanghai"))
    supplier.publish_reference(config.reference_root / "company_st.sqlite3", now.date(), config.sessions)
    started, written = time.monotonic(), _write_bytes()
    stage_times = _StageTimes()
    with DatabaseMeasurement(config.history_root) as measurement:
        result = RunHistorySync(
            config,
            supplier,
            clock=_clock(now),
            write_tuning=tuning,
            progress=stage_times,
            storage_kind=storage_kind or StorageProbe().inspect(config.history_root).kind,
        )
    rows = 0
    for path in config.history_root.glob("prices_[0-9][0-9][0-9][0-9].sqlite3"):
        with (
            SqliteSession(path, SqliteConnectionOptions(read_only=True)) as connection,
            SqliteTransaction(connection),
        ):
            rows += connection.run("SELECT COUNT(*) FROM daily_price_revision").require_one()[0]
    manifest = SQLitePublishedHistorySnapshot(config.history_root).manifest()
    return {
        "case": name,
        "seconds": round(time.monotonic() - started, 3),
        "physical_write_bytes": _write_bytes() - written,
        "state": result.state,
        "reason": result.reason,
        "requests": len(supplier.requests),
        "requested_rows": sum(supplier.requests),
        "rows": rows,
        "publication_hash": manifest.snapshot_hash if manifest is not None else None,
        "gaps": result.unresolved_price_cells,
        "files_bytes": sum(path.stat().st_size for path in config.history_root.rglob("*") if path.is_file()),
        "phase_seconds": stage_times.summary(),
        "database": measurement.result,
    }


def MeasureHistoryWrites(
    size: int, sessions: int, tuning: HistoryWriteTuning | None = None, *, storage_kind: StorageKind | None = None
) -> dict[str, object]:
    days = tuple(date(2018, 1, 1) + timedelta(days=i) for i in range(sessions))
    supplier = HistoryFixtureSupplier(days, size)
    results = []
    with TemporaryDirectory(prefix="trader-history-write-") as scratch:
        root = Path(scratch) / "history"
        project_root = Path(__file__).resolve().parents[2]
        config = replace(
            LoadHistorySyncConfiguration(project_root, DataPaths(Path(scratch) / "data")),
            history_root=root,
            sessions=sessions,
            reread_sessions=min(5, sessions),
            minimum_free_bytes=0,
        )
        for name, added in (("initial", 0), ("next_day", 1), ("two_weeks", 10), ("same_day", 0)):
            if added:
                supplier.days += tuple(supplier.days[-1] + timedelta(days=i + 1) for i in range(added))
            results.append(_measure_case(name, config, supplier, tuning, storage_kind))
        # Seed committed annual rows without publishing the first snapshot.
        # The measured restart includes disjoint stocks, unlike an empty-file write probe.
        resume_config = replace(config, history_root=Path(scratch) / "resume")
        interrupted = _StageTimes(cancel_after=max(1, size // 2))
        now = datetime.combine(supplier.days[-1], daytime(20, 30), ZoneInfo("Asia/Shanghai"))
        supplier.publish_reference(config.reference_root / "company_st.sqlite3", now.date(), config.sessions)
        seeded = RunHistorySync(
            resume_config,
            supplier,
            clock=_clock(now),
            cancel_requested=lambda: interrupted.cancelled,
            write_tuning=HistoryWriteTuning(1, sessions, max_codes=1, max_cells=sessions),
            progress=interrupted,
            storage_kind=storage_kind or StorageProbe().inspect(resume_config.history_root).kind,
        )
        if seeded.state != "cancelled":
            raise RuntimeError("history_write_resume_seed_failed")
        results.append(_measure_case("initial_resume", resume_config, supplier, tuning, storage_kind))
        storage_kind = storage_kind or StorageProbe().inspect(root).kind
    return {
        "schema_version": "history-write-benchmark",
        "status": "passed"
        if all(item["state"] in {"completed", "already_current"} and not item["gaps"] for item in results)
        else "failed",
        "summary": {
            "fixture": True,
            "stocks": size,
            "sessions": sessions,
            "storage_kind": storage_kind,
            "process_peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
            "write_tuning": (
                {"flush_codes": tuning.flush_codes, "flush_cells": tuning.flush_cells} if tuning else "auto"
            ),
            "cases": results,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-checkout", type=Path)
    parser.add_argument("--comparison-rounds", type=int, default=5)
    parser.add_argument("--size", type=int, default=8)
    parser.add_argument("--sessions", type=int, default=2000)
    parser.add_argument("--flush-codes", type=int)
    parser.add_argument("--flush-cells", type=int)
    parser.add_argument("--storage-kind", choices=("ssd", "hdd", "unknown"), default=None)
    args = parser.parse_args()
    if args.baseline_checkout is not None:
        from .storage_layout_comparison import CompareLayouts

        report = CompareLayouts(
            args.baseline_checkout, rounds=args.comparison_rounds, size=args.size, sessions=args.sessions
        )
        emit_report(report)
        return 0 if report["status"] == "passed" else 1
    if not 1 <= args.size <= 64 or not 5 <= args.sessions <= 2000:
        parser.error("size must be 1..64 and sessions 5..2000")
    if (args.flush_codes is None) != (args.flush_cells is None):
        parser.error("flush-codes and flush-cells must be set together")
    if args.flush_codes is not None and (not 1 <= args.flush_codes <= 64 or not 1 <= args.flush_cells <= 32768):
        parser.error("flush thresholds must fit the bounded pipeline")
    tuning = HistoryWriteTuning(args.flush_codes, args.flush_cells) if args.flush_codes is not None else None
    report = MeasureHistoryWrites(args.size, args.sessions, tuning, storage_kind=args.storage_kind)
    emit_report(report)
    return int(report["status"] != "passed")


if __name__ == "__main__":
    raise SystemExit(main())
