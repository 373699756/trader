"""Repeatable offline first-build and incremental-write measurements outside active data."""

from __future__ import annotations

import argparse
import resource
import sqlite3
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
    BaoStockSecurity,
    BaoStockSourceVersions,
)
from trader.download.domain.history_reference import HistoryStEvidence
from trader.download.domain.history_sync import HistorySupplierContext, HistorySyncProgress
from trader.download.domain.published_history import PublishedHistoryWindow, project_history_cell
from trader.download.infra.download_settings import load_history_sync_configuration
from trader.download.infra.history_archive_sync import run_history_sync
from trader.download.infra.history_storage_profile import (
    HistoryWriteTuning,
    classify_history_storage,
)

from .reporting import emit_report


@dataclass
class _FixtureSupplier:
    days: tuple[date, ...]
    size: int
    requests: list[int] = field(default_factory=list)

    def load_context(self, as_of: date, sessions: int) -> HistorySupplierContext:
        universe = tuple(
            BaoStockSecurity(f"{600000 + i}", "fixture", "main", date(1990, 1, 1), None, "fixture")
            for i in range(self.size)
        )
        return HistorySupplierContext(
            BaoStockCalendar(self.days[-sessions:]),
            universe,
            BaoStockSourceVersions("fixture", "3.10", ()),
            (),
            tuple(HistoryStEvidence(item.code, as_of, "clear") for item in universe),
        )

    def fetch_tencent_window(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> PublishedHistoryWindow:
        self.requests.append(len(dates))
        cells = self.fetch_baostock_prices(security, dates).batch.cells
        return PublishedHistoryWindow(security.code, tuple(project_history_cell(cell) for cell in cells))

    def fetch_baostock_prices(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> BaoStockCodeDownload:
        cells = []
        for day in dates:
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


@dataclass
class _StageTimes:
    starts: dict[str, float] = field(default_factory=dict)
    ends: dict[str, float] = field(default_factory=dict)
    lock: LockType = field(default_factory=Lock)

    def publish(self, progress: HistorySyncProgress) -> None:
        if progress.stage not in {"loading_context", "tencent_history", "sealing_partitions", "publishing_snapshot"}:
            return
        with self.lock:
            now = time.monotonic()
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
        return result


def _write_bytes() -> int:
    path = Path("/proc/self/io")
    if not path.exists():
        return 0
    return next(int(line.split(":")[1]) for line in path.read_text().splitlines() if line.startswith("write_bytes:"))


def _clock(observed: datetime) -> Callable[[], datetime]:
    return lambda: observed


def measure(size: int, sessions: int, tuning: HistoryWriteTuning | None = None) -> dict[str, object]:
    days = tuple(date(2018, 1, 1) + timedelta(days=i) for i in range(sessions))
    supplier = _FixtureSupplier(days, size)
    results = []
    with TemporaryDirectory(prefix="trader-history-write-") as scratch:
        root = Path(scratch) / "history"
        project_root = Path(__file__).resolve().parents[2]
        config = replace(
            load_history_sync_configuration(project_root, Path(scratch) / "reference"),
            archive_root=root,
            sessions=sessions,
            reread_sessions=min(5, sessions),
            minimum_free_bytes=0,
        )
        for name, added in (("initial", 0), ("next_day", 1), ("two_weeks", 10), ("same_day", 0)):
            if added:
                supplier.days += tuple(supplier.days[-1] + timedelta(days=i + 1) for i in range(added))
            supplier.requests.clear()
            now = datetime.combine(supplier.days[-1], daytime(20, 30), ZoneInfo("Asia/Shanghai"))
            started, written = time.monotonic(), _write_bytes()
            stage_times = _StageTimes()
            result = run_history_sync(config, supplier, clock=_clock(now), write_tuning=tuning, progress=stage_times)
            rows = 0
            for path in root.glob("partitions/*/[0-9][0-9].sqlite3"):
                with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
                    rows += connection.execute("SELECT COUNT(*) FROM daily_records").fetchone()[0]
            results.append(
                {
                    "case": name,
                    "seconds": round(time.monotonic() - started, 3),
                    "physical_write_bytes": _write_bytes() - written,
                    "state": result.state,
                    "reason": result.reason,
                    "requests": len(supplier.requests),
                    "requested_rows": sum(supplier.requests),
                    "rows": rows,
                    "gaps": result.unresolved_price_cells,
                    "files_bytes": sum(path.stat().st_size for path in root.rglob("*") if path.is_file()),
                    "phase_seconds": stage_times.summary(),
                }
            )
        storage_kind = classify_history_storage(root)
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
    parser.add_argument("--size", type=int, default=8)
    parser.add_argument("--sessions", type=int, default=2000)
    parser.add_argument("--flush-codes", type=int)
    parser.add_argument("--flush-cells", type=int)
    args = parser.parse_args()
    if not 1 <= args.size <= 64 or not 5 <= args.sessions <= 2000:
        parser.error("size must be 1..64 and sessions 5..2000")
    if (args.flush_codes is None) != (args.flush_cells is None):
        parser.error("flush-codes and flush-cells must be set together")
    if args.flush_codes is not None and (not 1 <= args.flush_codes <= 64 or not 1 <= args.flush_cells <= 32768):
        parser.error("flush thresholds must fit the bounded pipeline")
    tuning = HistoryWriteTuning(args.flush_codes, args.flush_cells) if args.flush_codes is not None else None
    report = measure(args.size, args.sessions, tuning)
    emit_report(report)
    return int(report["status"] != "passed")


if __name__ == "__main__":
    raise SystemExit(main())
