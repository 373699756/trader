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
from contextlib import nullcontext
from dataclasses import replace
from datetime import date, datetime, timedelta
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from typing import TYPE_CHECKING, TypedDict, cast
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
                env={**os.environ, "PYTHONPATH": str(checkout / "src"), "PYTHONHASHSEED": "0"},
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
            "python_hash_seed": 0,
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
                env={**os.environ, "PYTHONPATH": str(checkout / "src"), "PYTHONHASHSEED": "0"},
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
        paired_ratios = [
            candidate[name]["milliseconds"] / baseline_run[name]["milliseconds"]
            for baseline_run, candidate in zip(runs["baseline"], runs["candidate"], strict=True)
        ]
        paired_ratio = statistics.median(paired_ratios)
        passed = passed and paired_ratio < (1.0 if name.startswith("read_") else 1.1)
        comparisons.append(
            {
                "case": name,
                "baseline_median_ms": before,
                "candidate_median_ms": after,
                "ratio": after / before,
                "paired_ratios": paired_ratios,
                "median_paired_ratio": paired_ratio,
                "baseline_identity_scans": statistics.median(run[name]["identity_scans"] for run in runs["baseline"]),
                "candidate_identity_scans": statistics.median(run[name]["identity_scans"] for run in runs["candidate"]),
                "baseline_connections": statistics.median(run[name]["connections"] for run in runs["baseline"]),
                "candidate_connections": statistics.median(run[name]["connections"] for run in runs["candidate"]),
                "baseline_transactions": statistics.median(run[name]["transactions"] for run in runs["baseline"]),
                "candidate_transactions": statistics.median(run[name]["transactions"] for run in runs["candidate"]),
                "baseline_median_cpu_ms": statistics.median(run[name]["process_cpu_ms"] for run in runs["baseline"]),
                "candidate_median_cpu_ms": statistics.median(run[name]["process_cpu_ms"] for run in runs["candidate"]),
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
            "adapter_stocks": min(size, 1000),
            "adapter_sessions": 20,
            "wal_peak_sampling_seconds": 0.005,
            "cache_policy": "fresh process, warm operating-system cache; alternating order",
            "python_hash_seed": 0,
            "comparison_policy": "median of within-round candidate/baseline ratios; 11 incremental writes per worker",
            "comparison": comparisons,
            "runs": runs,
        },
    }


def _window_worker(checkout: Path, size: int, sessions: int) -> dict[str, dict[str, float]]:
    sys.path[:0] = [str(checkout / "src"), str(checkout)]
    from trader.infra import sqlite_connection

    counters = {"connections": 0, "transactions": 0}
    original_open = sqlite_connection.OpenSqliteConnection

    def trace(sql: str) -> None:
        if sql.lstrip().upper().startswith("BEGIN"):
            counters["transactions"] += 1

    def open_connection(*args, **kwargs):  # type: ignore[no-untyped-def]
        connection = original_open(*args, **kwargs)
        counters["connections"] += 1
        connection.observe_statements(trace)
        return connection

    sqlite_connection.OpenSqliteConnection = open_connection
    from trader.download.domain.published_history import (
        PublishedHistoryCell,
        PublishedHistorySide,
        PublishedHistoryWindow,
    )
    from trader.download.infra.qfq_sqlite import SQLiteQfqWindowCache
    from trader.infra.sqlite_connection import SqliteConnection

    try:
        from trader.download.infra.download_settings import LoadQfqWindowConfiguration

        qfq_configuration = LoadQfqWindowConfiguration(checkout)
    except ImportError:
        qfq_configuration = None

    scans = 0
    original = SqliteConnection.run

    def observed(connection, sql, parameters=()):  # type: ignore[no-untyped-def]
        nonlocal scans
        if sql.startswith(
            (
                "SELECT code,content_hash,source,start_date,end_date FROM window_binding",
                "SELECT code,digest,source,first_day,cutoff FROM identities",
            )
        ):
            scans += 1
        return original(connection, sql, parameters)

    SqliteConnection.run = observed  # type: ignore[method-assign]  # Process-local SQL instrumentation.
    days = tuple(date(2026, 1, 1) + timedelta(days=offset) for offset in range(sessions))
    result: dict[str, dict[str, float]] = {}

    def measure(name: str, action: Callable[[], object], *, repetitions: int = 1) -> None:
        before = scans
        before_counters = dict(counters)
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
        timings = []
        cpu_timings = []
        try:
            for _ in range(repetitions):
                started = time.perf_counter()
                cpu_started = time.process_time()
                action()
                timings.append((time.perf_counter() - started) * 1000)
                cpu_timings.append((time.process_time() - cpu_started) * 1000)
        finally:
            stopped.set()
            sampler.join(timeout=5)
        result[name] = {
            "milliseconds": statistics.median(timings),
            "process_cpu_ms": statistics.median(cpu_timings),
            "repetitions": repetitions,
            "identity_scans": (scans - before) / repetitions,
            "connections": (counters["connections"] - before_counters["connections"]) / repetitions,
            "transactions": (counters["transactions"] - before_counters["transactions"]) / repetitions,
            "sampled_wal_peak_bytes": wal_peak,
            "process_peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
        }

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
        root = Path(scratch)
        primary = SQLiteQfqWindowCache(root / "daily_window.sqlite3", "v2")
        peer = SQLiteQfqWindowCache(primary.path, "v3")

        def publish() -> None:
            for start in range(0, size, 32):
                windows = tuple(window(number) for number in range(start, min(start + 32, size)))
                if qfq_configuration is None:
                    primary.replace_paired_windows(
                        peer, windows, "fixture", calendar=days, completed_day=days[-1]
                    )  # type: ignore[call-arg]  # Retired checkout API.
                else:
                    primary.replace_paired_windows(
                        peer,
                        windows,
                        "fixture",
                        qfq_configuration,
                        calendar=days,
                        completed_day=days[-1],
                    )

        def read(cache: SQLiteQfqWindowCache) -> None:
            manifest = cache.manifest()
            assert manifest is not None
            profile_sessions = len(manifest.calendar_dates)
            assert sum(len(item.cells) for item in cache.iter_windows(manifest, sessions=profile_sessions)) == (
                size * min(sessions, profile_sessions)
            )

        measure("initial_write", publish)
        for cache in (primary, peer):
            measure(f"read_{cache.profile}", partial(read, cache))
        changed_price = 12.0

        def update() -> None:
            nonlocal changed_price
            changed_price += 1.0
            if qfq_configuration is None:
                changed = primary.replace_paired_windows(
                    peer, (window(0, changed_price),), "fixture", calendar=days, completed_day=days[-1]
                )  # type: ignore[call-arg]  # Retired checkout API.
            else:
                changed = primary.replace_paired_windows(
                    peer,
                    (window(0, changed_price),),
                    "fixture",
                    qfq_configuration,
                    calendar=days,
                    completed_day=days[-1],
                )
            assert changed[1] == sessions

        measure("incremental_write", update, repetitions=11)
        _sqlite_adapter_cases(root, min(size, 1000), measure, checkout=checkout)
    return result


def _sqlite_adapter_cases(
    root: Path, size: int, measure: Callable[[str, Callable[[], object]], None], *, checkout: Path
) -> None:
    """Exercise unchanged adapter contracts against each checkout, using identical typed inputs."""
    from trader.download.domain.baostock_daily import BaoStockDailyCell, BaoStockDailySide
    from trader.download.domain.company_evidence import Disclosure, DisclosurePage
    from trader.download.domain.history_revision import HistoryRevision
    from trader.download.infra.company_document_files import CompanyDocumentFiles
    from trader.download.infra.company_evidence_index import SQLiteCompanyEvidenceIndex
    from trader.download.infra.company_facts_reader import SQLiteCompanyEvidenceReader
    from trader.download.infra.history_prices import HistoryPricePartition
    from trader.training.infra.profile.v3.contracts import TOMORROW_HEAD_CONTRACT
    from trader.training.infra.sample_index import SQLiteTrainingSampleIndex, TrainingSample

    document_options: dict[str, object] = {}
    index_options: dict[str, object] = {}
    reader_options: dict[str, object] = {}
    if checkout == Path(__file__).resolve().parents[2]:
        from trader.download.infra.download_settings import LoadCompanySyncConfiguration
        from trader.infra.settings import LoadStrategySettings

        company_configuration = LoadCompanySyncConfiguration(checkout)
        event_policy = LoadStrategySettings(checkout / "config/strategy.json").company_scoring.events
        document_options = {
            "max_text_characters": company_configuration.extended_document_limits.max_text_characters,
            "max_bytes": company_configuration.temporary_text_capacity_bytes,
            "reserve_bytes": company_configuration.temporary_disk_reserve_bytes,
        }
        index_options = {"event_policy": event_policy}
        reader_options = {"event_policy": event_policy}

    codes = tuple(f"600{number:03d}" for number in range(size))
    days = tuple(date(2026, 1, day) for day in range(1, 21))
    revisions = []
    for day in days:
        for code in codes:
            raw = BaoStockDailySide(
                code, day, "unadjusted", 10.0, 10.0, 10.0, 10.0, 100.0, 1000.0, 10.0, 0.0, 0.0, "trading"
            )
            qfq = replace(raw, adjustment="qfq", preclose=None, pct_change=None, turnover=None)
            revisions.append(
                HistoryRevision(1, "main", BaoStockDailyCell(code, day, "complete", raw, qfq), None, None, None)
            )
    partition = HistoryPricePartition(root / "prices_2026.sqlite3", 2026, 1)
    partition.initialize()
    measure("history_batch_write", lambda: partition.save_revisions(revisions))

    def scan() -> None:
        assert sum(1 for _ in partition.iter_training_rows(days[0], days[-1], snapshot_sequence=1)) == size * 20

    measure("history_training_narrow_read", scan)
    split_days = tuple(date(2021, 1, 1) + timedelta(days=offset) for offset in range(1600))
    if (checkout / "src/trader/training/domain/training_dates.py").is_file():
        from trader.training.domain.training_dates import BuildTrainingDateSplit

        split = BuildTrainingDateSplit(split_days, parent_manifest_hash="a" * 64)
    else:
        # Only the isolated historical checkout owns this retired API.
        from trader.download.domain.baostock_daily import BuildBaostockTrainingSplit  # type: ignore[attr-defined]  # Historical checkout only.

        split = BuildBaostockTrainingSplit(split_days, parent_manifest_hash="a" * 64)
    samples = tuple(
        TrainingSample(code, day, "main", "fixture", 1_000_000.0, (0.1,) * 6, (0.02, 0.03, 0.04, 0.05, 0.06, 0.045))
        for code in codes
        for day in split.model_fit_dates[:20]
    )
    from trader.training.infra.sample_index import TrainingIndustryData

    sample_path = root / "samples.sqlite3"
    named = (checkout / "src/trader/training/infra/sample_schema.py").is_file()
    features = TOMORROW_HEAD_CONTRACT.feature_manifest.names if named else 6

    def build_samples() -> None:
        with SQLiteTrainingSampleIndex(sample_path, features) as index:  # type: ignore[arg-type]  # Historical checkout only.
            index.add_final(samples)
            index.prepare_for_model_fitting(split)

    def read_samples() -> None:
        with SQLiteTrainingSampleIndex(sample_path, features, prepared_split_hash=split.content_hash) as index:  # type: ignore[arg-type]
            counts = index.industry_counts(TOMORROW_HEAD_CONTRACT)
            assert len(counts) == 1
            data = index.industry_data(counts[0], TOMORROW_HEAD_CONTRACT)
            with data if named else nullcontext(cast(TrainingIndustryData, data)) as matrix:
                assert matrix.training.features.shape == (size * 20, 6)
                assert abs(float(matrix.training.labels.sum()) - size * 20 * 0.02) < 1e-6

    measure("training_sample_write", build_samples)
    measure("training_sample_read", read_samples)
    now = datetime(2026, 10, 18, 9, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
    company = SQLiteCompanyEvidenceIndex(
        root / "company.sqlite3",
        facts_path=root / "company_facts.sqlite3",
        documents=CompanyDocumentFiles(root / "text", **document_options),
        **index_options,
    )
    company.initialize()
    disclosure_partition = company.partitions(("sse",), now)[0]
    disclosures = tuple(
        Disclosure("fixture", f"{code}:{offset}", (code,), "普通公告", now, "", str(offset))
        for code in codes
        for offset in range(3)
    )
    measure(
        "company_page_write",
        lambda: company.commit_page(disclosure_partition, DisclosurePage(disclosures, len(disclosures), False), now),
    )
    measure("company_publish", lambda: company.publish(codes, now))
    reader = SQLiteCompanyEvidenceReader(company.facts_path, raw_path=company.path, **reader_options)
    expected = reader.read(codes)

    def read_company() -> None:
        assert reader.read(codes) == expected

    measure("company_read", read_company)


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
            connection.set_trace_callback(trace)  # type: ignore[attr-defined]  # Retired checkout returns the driver.
        return connection

    sqlite_connection.OpenSqliteConnection = observed_open
    if TYPE_CHECKING or layout == "annual":
        from scripts.runtime_diagnostics.history_write_benchmark import HistoryFixtureSupplier as SupplierBase
    else:
        # This worker imports the retired checkout's fixture, whose API is unchanged there.
        from scripts.runtime_diagnostics.history_write_benchmark import _FixtureSupplier as SupplierBase
    from trader.download.domain.history_sync import HistorySupplierContext, HistorySyncConfiguration
    from trader.download.infra.history_control_index import HistoryControlIndex
    from trader.download.infra.history_snapshot_reader import HistorySnapshotReader
    from trader.download.infra.history_sync_runner import RunHistorySync
    from trader.download.infra.published_history_snapshot import SQLitePublishedHistorySnapshot
    from trader.download.infra.qfq_sqlite import SQLiteQfqWindowCache
    from trader.recommendation.infra.market_data.published_history_bars import QfqBars

    codes = tuple(f"{('600', '000', '300', '688')[index % 4]}{index // 4:03d}" for index in range(size))
    days = tuple(date(2018 + month // 12, month % 12 + 1, day) for month in range(100) for day in range(1, 21))

    class Supplier(SupplierBase):
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
        if layout == "monthly":
            config = HistorySyncConfiguration(  # type: ignore[call-arg]  # Historical checkout only.
                history,
                reference_root=root / "reference",
                training_root=root / "models",
                sessions=2000,
                minimum_free_bytes=0,
            )
        else:
            config = HistorySyncConfiguration(
                history, reference_root=root / "reference", sessions=2000, minimum_free_bytes=0
            )
        qfq_configuration = None
        if layout == "annual":
            from trader.download.infra.download_settings import LoadQfqWindowConfiguration

            qfq_configuration = LoadQfqWindowConfiguration(checkout)
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
                if layout == "annual":
                    supplier.publish_reference(config.reference_root / "company_st.sqlite3", at.date(), config.sessions)
                result = RunHistorySync(config, supplier, clock=lambda: at)
                if result.state not in ("completed", "already_current") or result.unresolved_price_cells:
                    raise RuntimeError(f"fixture synchronization failed: {result.state}/{result.reason}")
                return result

            measure("history_initial_write", sync)
            index = HistoryControlIndex(history / ("catalog.sqlite3" if layout == "annual" else "control.sqlite3"))
            publication = SQLitePublishedHistorySnapshot(history)
            manifest = publication.manifest()
            assert manifest is not None
            snapshot = next(
                item for item in index.load_state().snapshots if item.content_hash == manifest.snapshot_hash
            )

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
            maximum_sessions = qfq_configuration.maximum_sessions if qfq_configuration is not None else 251
            calendar = supplier.days[-maximum_sessions:]
            windows = tuple(supplier.fetch_tencent_window(security, calendar) for security in context.universe)

            def publish() -> object:
                if layout == "annual":
                    assert qfq_configuration is not None
                    return primary.replace_paired_windows(
                        secondary,
                        windows,
                        "fixture",
                        qfq_configuration,
                        calendar=calendar,
                        completed_day=calendar[-1],
                    )
                return primary.replace_paired_windows(secondary, windows, "fixture")  # type: ignore[call-arg]  # Retired API.

            measure("window_initial_write", publish)

            def projection() -> None:
                configured_sessions = (
                    (qfq_configuration.v2_sessions, qfq_configuration.v3_sessions)
                    if qfq_configuration is not None
                    else (251, 61)
                )
                for cache, length in zip((primary, secondary), configured_sessions, strict=True):
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
                        replace(window.cells[-1], qfq=replace(window.cells[-1].qfq, close_price=9.5)),  # type: ignore[type-var]  # Fixture always has paired prices.
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
