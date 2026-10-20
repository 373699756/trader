"""Isolated, alternating measurements of the retired and current persistence layouts.

Invoked by history_write_benchmark. A worker imports exactly one explicit checkout;
legacy API selection exists only in this offline comparison, never in product code.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import statistics
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime, timedelta
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from typing import TypedDict
from zoneinfo import ZoneInfo


class _MeasuredCase(TypedDict):
    milliseconds: float
    connections: int
    transactions: int


class _LayoutRun(TypedDict):
    cases: dict[str, _MeasuredCase]
    history_database_files: int
    window_database_files: int
    auxiliary_files: int
    logical_bytes: int
    allocated_bytes: int
    sampled_wal_peak_bytes: int
    peak_rss_bytes: int


def CompareLayouts(baseline: Path, *, rounds: int, size: int, sessions: int = 2000) -> dict[str, object]:
    current = Path(__file__).resolve().parents[2]
    baseline = baseline.resolve(strict=True)
    if baseline == current or current in baseline.parents or not (baseline / "src/trader").is_dir():
        raise ValueError("baseline must be an isolated checkout outside the active repository")
    if (baseline / "src/trader/download/infra/history_prices.py").is_file():
        return _compare_publications(baseline, current, rounds=rounds, size=size, sessions=min(sessions, 251))
    if not 5 <= rounds <= 10 or not 4 <= size <= 64:
        raise ValueError("comparison requires 5..10 rounds and 4..64 securities")
    runs: dict[str, list[_LayoutRun]] = {"monthly": [], "annual": []}
    for number in range(rounds):
        order = ("monthly", "annual") if number % 2 == 0 else ("annual", "monthly")
        for layout in order:
            checkout = baseline if layout == "monthly" else current
            completed = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--worker",
                    layout,
                    "--checkout",
                    str(checkout),
                    "--size",
                    str(size),
                ],
                cwd=checkout,
                env={**os.environ, "PYTHONPATH": str(checkout / "src")},
                capture_output=True,
                text=True,
                timeout=600,
                check=True,
            )
            runs[layout].append(json.loads(completed.stdout))
    required_improvements = {
        "history_startup_load",
        "history_hot_read",
        "training_narrow_scan",
        "history_incremental_write",
        "window_hot_projection",
        "window_incremental_write",
    }
    comparison = []
    for name in runs["monthly"][0]["cases"]:
        old = statistics.median(run["cases"][name]["milliseconds"] for run in runs["monthly"])
        new = statistics.median(run["cases"][name]["milliseconds"] for run in runs["annual"])
        comparison.append(
            {
                "case": name,
                "baseline_median_ms": old,
                "candidate_median_ms": new,
                "ratio": new / old,
                "within_regression_limit": new <= old * 1.1,
                "improvement_required": name in required_improvements,
                "improved": new < old,
            }
        )
    return {
        "schema_version": "storage-layout-comparison",
        "status": "passed"
        if all(
            item["within_regression_limit"] and (not item["improvement_required"] or item["improved"])
            for item in comparison
        )
        else "failed",
        "summary": {
            "fixture": True,
            "rounds": rounds,
            "stocks": size,
            "history_months": 100,
            "history_sessions": 2000,
            "cache_policy": "fresh process, warm operating-system cache; alternating order",
            "wal_peak_sampling_seconds": 0.005,
            "comparison": comparison,
            "runs": runs,
        },
    }


def _compare_publications(baseline: Path, current: Path, *, rounds: int, size: int, sessions: int) -> dict[str, object]:
    if not 5 <= rounds <= 10 or not 4 <= size <= 5000 or not 1 <= sessions <= 251:
        raise ValueError("window comparison requires 5..10 rounds, 4..5000 securities and 1..251 sessions")
    runs: dict[str, list[dict[str, dict[str, float]]]] = {"baseline": [], "candidate": []}
    for number in range(rounds):
        order = ("baseline", "candidate") if number % 2 == 0 else ("candidate", "baseline")
        for label in order:
            checkout = baseline if label == "baseline" else current
            completed = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--worker",
                    "window",
                    "--checkout",
                    str(checkout),
                    "--size",
                    str(size),
                    "--sessions",
                    str(sessions),
                ],
                cwd=checkout,
                env={**os.environ, "PYTHONPATH": str(checkout / "src")},
                capture_output=True,
                text=True,
                timeout=600,
                check=True,
            )
            runs[label].append(json.loads(completed.stdout))
    comparisons = []
    passed = True
    for name in runs["baseline"][0]:
        before = statistics.median(run[name]["milliseconds"] for run in runs["baseline"])
        after = statistics.median(run[name]["milliseconds"] for run in runs["candidate"])
        passed = passed and after < before * (1.0 if name.startswith("read_") else 1.1)
        comparisons.append(
            {
                "case": name,
                "baseline_median_ms": before,
                "candidate_median_ms": after,
                "ratio": after / before,
                "baseline_identity_scans": statistics.median(run[name]["identity_scans"] for run in runs["baseline"]),
                "candidate_identity_scans": statistics.median(run[name]["identity_scans"] for run in runs["candidate"]),
            }
        )
    return {
        "schema_version": "storage-layout-comparison",
        "status": "passed" if passed else "failed",
        "summary": {
            "fixture": True,
            "mode": "window_publications",
            "rounds": rounds,
            "stocks": size,
            "sessions": sessions,
            "cache_policy": "fresh process, warm operating-system cache; alternating order",
            "comparison": comparisons,
            "runs": runs,
        },
    }


def _window_worker(checkout: Path, size: int, sessions: int) -> dict[str, dict[str, float]]:
    sys.path[:0] = [str(checkout / "src"), str(checkout)]
    from trader.download.domain.published_history import (
        PublishedHistoryCell,
        PublishedHistorySide,
        PublishedHistoryWindow,
    )
    from trader.download.infra.qfq_sqlite import SQLiteQfqWindowCache
    from trader.infra.sqlite_connection import SqliteConnection

    scans = 0
    original = SqliteConnection.run

    def observed(connection, sql, parameters=()):  # type: ignore[no-untyped-def]
        nonlocal scans
        if sql.startswith("SELECT code,digest,source,first_day,cutoff FROM identities"):
            scans += 1
        return original(connection, sql, parameters)

    SqliteConnection.run = observed
    days = tuple(date(2026, 1, 1) + timedelta(days=offset) for offset in range(sessions))
    result: dict[str, dict[str, float]] = {}

    def measure(name: str, action: Callable[[], object]) -> None:
        before = scans
        started = time.perf_counter()
        action()
        result[name] = {"milliseconds": (time.perf_counter() - started) * 1000, "identity_scans": scans - before}

    def window(number: int, price: float = 10.0) -> PublishedHistoryWindow:
        code = f"{('600', '601', '603', '605', '000')[number // 1000]}{number % 1000:03d}"
        cells = []
        for day in days:
            raw = PublishedHistorySide(
                code, day, "unadjusted", price, price, price, price, 100.0, 1000.0, None, None, None, "trading"
            )
            cells.append(PublishedHistoryCell(code, day, "complete", raw, replace(raw, adjustment="qfq")))
        return PublishedHistoryWindow(code, tuple(cells))

    with TemporaryDirectory(prefix="trader-window-comparison-") as scratch:
        primary = SQLiteQfqWindowCache(Path(scratch) / "daily_window.sqlite3", "v2")
        peer = SQLiteQfqWindowCache(primary.path, "v3")

        def publish() -> None:
            for start in range(0, size, 32):
                primary.replace_paired_windows(
                    peer,
                    tuple(window(number) for number in range(start, min(start + 32, size))),
                    "fixture",
                    calendar=days,
                    completed_day=days[-1],
                )

        def read(cache: SQLiteQfqWindowCache) -> None:
            manifest = cache.manifest()
            assert manifest is not None
            assert sum(len(item.cells) for item in cache.iter_windows(manifest, sessions=cache.sessions)) == (
                size * min(sessions, cache.sessions)
            )

        measure("initial_write", publish)
        for cache in (primary, peer):
            measure(f"read_{cache.profile}", partial(read, cache))
        measure(
            "incremental_write",
            lambda: primary.replace_paired_windows(
                peer, (window(0, 12.0),), "fixture", calendar=days, completed_day=days[-1]
            ),
        )
    return result


def _worker(checkout: Path, layout: str, size: int) -> dict[str, object]:
    sys.path[:0] = [str(checkout / "src"), str(checkout)]
    from trader.infra import sqlite_connection

    counters = {"connections": 0, "transactions": 0}
    original_open = sqlite_connection.OpenSqliteConnection

    def trace(sql: str) -> None:
        if sql.lstrip().upper().startswith("BEGIN"):
            counters["transactions"] += 1

    def observed_open(*args, **kwargs):  # type: ignore[no-untyped-def]  # two checkout APIs return different handles
        counters["connections"] += 1
        connection = original_open(*args, **kwargs)
        if layout == "annual":
            connection.observe_statements(trace)
        else:
            connection.set_trace_callback(trace)
        return connection

    sqlite_connection.OpenSqliteConnection = observed_open
    from scripts.runtime_diagnostics.history_write_benchmark import _FixtureSupplier
    from trader.download.domain.history_sync import HistorySupplierContext, HistorySyncConfiguration
    from trader.download.infra.history_control_index import HistoryControlIndex
    from trader.download.infra.history_snapshot_reader import HistorySnapshotReader
    from trader.download.infra.history_sync_runner import RunHistorySync
    from trader.download.infra.published_history_snapshot import SQLitePublishedHistorySnapshot
    from trader.download.infra.qfq_sqlite import SQLiteQfqWindowCache
    from trader.recommendation.infra.market_data.published_history_bars import QfqBars

    codes = tuple(f"{('600', '000', '300', '688')[index % 4]}{index // 4:03d}" for index in range(size))
    days = tuple(date(2018 + month // 12, month % 12 + 1, day) for month in range(100) for day in range(1, 21))

    class Supplier(_FixtureSupplier):
        def load_context(self, as_of: date, sessions: int) -> HistorySupplierContext:
            context = super().load_context(as_of, sessions)
            universe = tuple(
                replace(
                    security,
                    code=code,
                    board="chinext" if code.startswith("300") else "star" if code.startswith("688") else "main",
                )
                for security, code in zip(context.universe, codes, strict=True)
            )
            evidence = tuple(replace(item, code=code) for item, code in zip(context.st_evidence, codes, strict=True))
            return replace(context, universe=universe, st_evidence=evidence)

    supplier = Supplier(days, size)
    cases: dict[str, _MeasuredCase] = {}

    def measure(name: str, action: Callable[[], object]) -> object:
        before = dict(counters)
        started = time.perf_counter()
        value = action()
        elapsed = (time.perf_counter() - started) * 1000
        cases[name] = {
            "milliseconds": elapsed,
            "connections": counters["connections"] - before["connections"],
            "transactions": counters["transactions"] - before["transactions"],
        }
        return value

    with TemporaryDirectory(prefix="trader-layout-comparison-") as scratch:
        root = Path(scratch)
        history = root / "history"
        config = HistorySyncConfiguration(
            history,
            reference_root=root / "reference",
            training_root=root / "models",
            sessions=2000,
            minimum_free_bytes=0,
        )
        stopped = Event()
        wal_peak = 0

        def sample_wal() -> None:
            nonlocal wal_peak
            while not stopped.wait(0.005):
                total = 0
                for path in root.rglob("*-wal"):
                    try:
                        total += path.stat().st_size
                    except FileNotFoundError:
                        continue
                wal_peak = max(wal_peak, total)

        sampler = Thread(target=sample_wal, name="offline-wal-sampler")
        sampler.start()
        try:

            def sync() -> object:
                at = datetime.combine(supplier.days[-1], datetime.min.time(), ZoneInfo("Asia/Shanghai")).replace(
                    hour=20
                )
                result = RunHistorySync(config, supplier, clock=lambda: at)
                if result.state not in ("completed", "already_current") or result.unresolved_price_cells:
                    raise RuntimeError(f"fixture synchronization failed: {result.state}/{result.reason}")
                return result

            measure("history_initial_write", sync)
            index = HistoryControlIndex(history / ("catalog.sqlite3" if layout == "annual" else "control.sqlite3"))
            snapshot = index.load_state().active_snapshot
            assert snapshot is not None
            publication = SQLitePublishedHistorySnapshot(history)

            def load() -> object:
                manifest = publication.manifest()
                assert manifest is not None
                windows = publication.read_windows(manifest, codes, sessions=251)
                assert sum(len(window.cells) for window in windows) == size * 251
                return windows

            measure("history_startup_load", load)
            measure("history_hot_read", load)
            reader = HistorySnapshotReader(history)

            def scan() -> None:
                assert sum(1 for _ in reader.iter_training_rows(snapshot, days, codes)) == size * 2000

            measure("training_narrow_scan", scan)
            supplier.days += (days[-1] + timedelta(days=1),)
            measure("history_incremental_write", sync)
            measure("history_unchanged_reuse", sync)
            daily = root / "daily_window.sqlite3" if layout == "annual" else root / "qfq"
            primary, secondary = (SQLiteQfqWindowCache(daily, profile) for profile in ("v2", "v3"))
            context = supplier.load_context(supplier.days[-1], 2000)
            calendar = supplier.days[-251:]
            windows = tuple(supplier.fetch_tencent_window(security, calendar) for security in context.universe)

            def publish() -> object:
                if layout == "annual":
                    return primary.replace_paired_windows(
                        secondary, windows, "fixture", calendar=calendar, completed_day=calendar[-1]
                    )
                return primary.replace_paired_windows(secondary, windows, "fixture")

            measure("window_initial_write", publish)

            def projection() -> None:
                for cache, length in ((primary, 251), (secondary, 61)):
                    manifest = cache.manifest()
                    assert manifest is not None
                    loaded = cache.read_codes(codes)
                    assert sum(len(QfqBars(window)) for window in loaded) == size * length

            measure("window_startup_projection", projection)
            measure("window_hot_projection", projection)
            measure("window_unchanged_reuse", publish)
            windows = tuple(
                replace(
                    window,
                    cells=(
                        *window.cells[:-1],
                        replace(window.cells[-1], qfq=replace(window.cells[-1].qfq, close_price=9.5)),
                    ),
                )
                for window in windows
            )
            measure("window_incremental_write", publish)
        finally:
            stopped.set()
            sampler.join(timeout=5)
        files = tuple(path for path in root.rglob("*") if path.is_file())
        return {
            "cases": cases,
            "history_database_files": len(tuple(history.rglob("*.sqlite3"))),
            "window_database_files": 1 if layout == "annual" else len(tuple(daily.rglob("*.sqlite3"))),
            "auxiliary_files": sum(path.suffix != ".sqlite3" for path in files),
            "logical_bytes": sum(path.stat().st_size for path in files),
            "allocated_bytes": sum(path.stat().st_blocks * 512 for path in files),
            "sampled_wal_peak_bytes": wal_peak,
            "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", choices=("monthly", "annual", "window"), required=True)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--size", type=int, required=True)
    parser.add_argument("--sessions", type=int, default=251)
    args = parser.parse_args()
    checkout = args.checkout.resolve(strict=True)
    report = (
        _window_worker(checkout, args.size, args.sessions)
        if args.worker == "window"
        else _worker(checkout, args.worker, args.size)
    )
    print(json.dumps(report, sort_keys=True))
