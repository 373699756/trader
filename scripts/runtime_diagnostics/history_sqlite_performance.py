"""Read-only physical layout summary for the published monthly history SQLite databases."""

from __future__ import annotations

import argparse
import calendar
import cProfile
import json
import os
import platform
import pstats
import resource
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime
from functools import partial
from zoneinfo import ZoneInfo
from itertools import islice
from pathlib import Path

from trader.download.application.read_published_history import ReadPublishedHistoryUseCase
from trader.download.domain.history_control import HistorySnapshot, HistorySnapshotPartition
from trader.download.domain.history_revision import HistoryRevision
from trader.download.infra.history_control_index import HistoryControlError, HistoryControlIndex
from trader.download.infra.history_direct_prices import HistoryDirectPrices
from trader.download.infra.history_prices import HistoryPricePartition
from trader.download.infra.history_sqlite_activity import HistorySqliteActivity
from trader.download.infra.published_history_snapshot import SQLitePublishedHistorySnapshot
from trader.download.infra.company_facts_reader import SQLiteCompanyEvidenceReader
from trader.download.infra.current_industry_publication import CurrentIndustryCache
from trader.download.infra.reference_index import SQLiteReferenceIndex
from trader.infra.cache import BoundedLruCache
from trader.infra.settings import LoadRuntimeSettings, LoadStrategySettings
from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.infra.sqlite_codec import DecodeDate
from trader.infra.sqlite_connection import SqliteConnectionOptions, SqliteSession, SqliteTransaction
from trader.recommendation.infra.market_data.published_history_cache import PublishedHistoryCache
from trader.recommendation.infra.market_data.shared_history_projection import SharedHistoryProjection
from trader.recommendation.infra.market_data.published_company_research import PublishedCompanyResearch
from trader.recommendation.infra.market_data.published_references import PublishedReferences
from trader.recommendation.infra.scoring.profile_factory import LoadScoringProfile
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.recommendation.domain.scoring.profile_selection import ParseProfileSelection
from trader.training.infra.training_settings import LoadRuntimeTrainingSettings
from trader.recommendation.infra.runtime.initialization import FixedCacheTtl
from trader.recommendation.infra.normalization.features import FeatureBuilder
from trader.recommendation.domain.market.models import MarketQuote

from .reporting import emit_report
from .database_measurement import DatabaseMeasurement
from .diagnostic_types import PROJECT_ROOT


@dataclass(frozen=True)
class _PhysicalSummary:
    partition_count: int
    total_bytes: int
    page_count: int
    free_page_count: int
    overflow_bytes: int
    index_bytes: int
    page_sizes: tuple[int, ...]
    page_stat_partition_count: int


@dataclass(frozen=True)
class _QueryMeasurement:
    workload: str
    row_count: int
    cold_median_ms: float
    warm_median_ms: float
    temporary_btree: bool
    query_plan: tuple[str, ...]


@dataclass(frozen=True)
class _RevisionWriteMeasurement:
    requested_rows: int
    statement_count: int
    transaction_count: int
    elapsed_ms: float
    rows_per_second: float
    database_growth_bytes: int
    seal_ms: float


@dataclass(frozen=True)
class _HistorySQLitePerformanceReport:
    physical: _PhysicalSummary
    queries: tuple[_QueryMeasurement, ...]
    revision_write: _RevisionWriteMeasurement
    peak_rss_bytes: int


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history-root", type=Path, required=True)
    parser.add_argument("--inventory-only", action="store_true")
    parser.add_argument("--preload-audit", action="store_true")
    parser.add_argument("--preload-cpu-profile", action="store_true")
    parser.add_argument("--preload-feature-audit", action="store_true")
    parser.add_argument("--preload-profiles", type=ParseProfileSelection, default=("v1", "v2", "v3"))
    parser.add_argument("--runtime-config", type=Path, default=PROJECT_ROOT / "config/runtime.json")
    parser.add_argument("--resume-samples", type=int, default=0)
    parser.add_argument("--page-sample-count", type=int, default=1)
    parser.add_argument("--query-rounds", type=int, default=3)
    parser.add_argument("--revision-write-sample-count", type=int, default=512)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.preload_audit:
        return _measure_preload(args.history_root.resolve(), args.runtime_config, args.preload_profiles, cpu_profile=args.preload_cpu_profile, feature_audit=args.preload_feature_audit)
    if args.resume_samples:
        try:
            return _measure_resume_reads(args.history_root.resolve(), args.resume_samples)
        except (OSError, RuntimeError, sqlite3.Error, TypeError, ValueError):
            emit_report({"schema_version": "history_sqlite_performance", "status": "failed"})
            return 1
    if args.inventory_only:
        emit_report(
            {
                "schema_version": "history-space-inventory",
                "status": "passed",
                "summary": inspect_history_space(args.history_root.resolve()),
            }
        )
        return 0
    try:
        if not 1 <= args.page_sample_count <= 100 or not 1 <= args.query_rounds <= 9:
            raise ValueError("history SQLite sample counts are invalid")
        if not 1 <= args.revision_write_sample_count <= 5_000:
            raise ValueError("revision write sample count must be within 1..5000")
        report = inspect_history_sqlite_performance(
            args.history_root.resolve(),
            args.page_sample_count,
            args.query_rounds,
            args.revision_write_sample_count,
        )
    except (HistoryControlError, OSError, RuntimeError, sqlite3.Error, TypeError, ValueError):
        emit_report({"schema_version": "history_sqlite_performance", "status": "failed"})
        return 1
    summary = report.physical
    compact = summary.page_sizes == (8192,)
    payload = {
        "schema_version": "history_sqlite_performance",
        "status": "passed" if compact else "degraded",
        "summary": {
            "partition_count": summary.partition_count,
            "total_bytes": summary.total_bytes,
            "page_count": summary.page_count,
            "free_page_count": summary.free_page_count,
            "sampled_overflow_bytes": summary.overflow_bytes,
            "sampled_index_bytes": summary.index_bytes,
            "page_stat_partition_count": summary.page_stat_partition_count,
            "page_sizes": list(summary.page_sizes),
            "peak_rss_bytes": report.peak_rss_bytes,
            "queries": [
                {
                    "workload": item.workload,
                    "row_count": item.row_count,
                    "cold_median_ms": item.cold_median_ms,
                    "warm_median_ms": item.warm_median_ms,
                    "temporary_btree": item.temporary_btree,
                    "query_plan": list(item.query_plan),
                }
                for item in report.queries
            ],
            "revision_write": {
                "requested_rows": report.revision_write.requested_rows,
                "statement_count": report.revision_write.statement_count,
                "transaction_count": report.revision_write.transaction_count,
                "elapsed_ms": report.revision_write.elapsed_ms,
                "rows_per_second": report.revision_write.rows_per_second,
                "database_growth_bytes": report.revision_write.database_growth_bytes,
                "seal_ms": report.revision_write.seal_ms,
            },
        },
    }
    emit_report(payload)
    return 0


def _measure_preload(
    root: Path,
    runtime_config: Path,
    selected: tuple[ScoringProfileId, ...],
    *,
    cpu_profile: bool = False,
    feature_audit: bool = False,
) -> int:
    total_started = time.monotonic()
    settings = LoadRuntimeSettings(runtime_config)
    observed = datetime.now(ZoneInfo("Asia/Shanghai"))
    cache: BoundedLruCache[object] = BoundedLruCache(settings.market_data.cache_policy)
    reserve = partial(cache.reserve_resident, "security_master_calendar")
    reference_index = SQLiteReferenceIndex(settings.data_paths.reference_database)
    references = PublishedReferences(
        reference_index.read,
        reference_index.identity,
        CurrentIndustryCache(settings.data_paths.reference / "industry.sqlite3").load,
        max_age_seconds=FixedCacheTtl(settings, "security_master_calendar"),
        reserve_bytes=partial(reserve, "startup_reference"),
    )
    references.initialize()
    reference = references.reference()
    if reference is None:
        raise RuntimeError("startup_reference_unavailable")
    reference_seconds = time.monotonic() - total_started
    model_started = time.monotonic()
    models = tuple(LoadScoringProfile(profile, training_root=settings.data_paths.models) for profile in selected)
    model_seconds = time.monotonic() - model_started
    history = ReadPublishedHistoryUseCase(SQLitePublishedHistorySnapshot(root))
    manifest = history.manifest()
    calendar_dates = reference.open_dates
    owner = SharedHistoryProjection(history, monotonic=time.monotonic, reserve_bytes=partial(reserve, "qfq_shared_bars"))
    profiles = tuple(
        PublishedHistoryCache(
            history,
            lookback_sessions=LoadRuntimeTrainingSettings(settings.project_root, profile).history_sessions,
            learned_rank_inputs=profile == "v1",
            sequence_inputs=profile == "v5",
            require_input_binding=True,
            reserve_bytes=partial(reserve, f"qfq_{profile}"),
            projection_owner=owner,
            open_dates=lambda: calendar_dates,
            monotonic=time.monotonic,
        )
        for profile in selected
    )
    started = time.monotonic()
    profiler = cProfile.Profile()
    profiling = False

    def cancelled() -> bool:
        nonlocal profiling
        if cpu_profile and owner.window_count and not profiling:
            profiler.enable()
            profiling = True
        return False

    # A read-only preload does not need a background WAL sampler; keeping this
    # probe single-threaded also makes function profiling attributable.
    with DatabaseMeasurement(root, sample_wal=False) as measurement:
        owner.initialize(cancelled)
    profiler.disable()
    history_seconds = time.monotonic() - started
    company_seconds = binding_seconds = 0.0
    company_error: str | None = None
    company_profiler = cProfile.Profile()
    feature_measurement: dict[str, object] = {}
    if all(profile.status().state == "published" for profile in profiles):
        if cpu_profile:
            company_profiler.enable()
        company_started = time.monotonic()
        policy = LoadStrategySettings(settings.strategy_config_path, scoring_profile=selected[0])
        research = PublishedCompanyResearch(
            SQLiteCompanyEvidenceReader(settings.data_paths.company, raw_path=settings.data_paths.company_collection, event_policy=policy.company_scoring.events),
            wall_clock=lambda: observed,
            open_dates=lambda: calendar_dates,
            monotonic=time.monotonic,
            reserve_bytes=partial(reserve, "company"),
            source_versions=references.versions,
            config_version=settings.config_version,
            company_policy=policy.company_scoring,
        )
        research.initialize(manifest.universe_codes if manifest else (), cancelled=lambda: False)
        company_seconds = time.monotonic() - company_started
        company_error = research.status().last_error or None
        binding_started = time.monotonic()
        for profile, model in zip(profiles, models, strict=True):
            profile.bind_inputs(observed, versions={**references.versions(), "company": research.loaded_identity, "model": CanonicalDigest(model.publications)}, config_version=settings.config_version)
            profile.record_projection_observation()
        binding_seconds = time.monotonic() - binding_started
        company_profiler.disable()
        if feature_audit:
            feature_measurement = _measure_features(runtime_config, selected, profiles, research, references, cache, observed)
    stats = pstats.Stats(profiler).get_stats_profile() if profiling else None
    functions = [
        {
            "function": name,
            "module": Path(value.file_name).name,
            "calls": value.ncalls,
            "self_seconds": value.tottime,
            "cumulative_seconds": value.cumtime,
        }
        for name, value in (sorted(stats.func_profiles.items(), key=lambda item: item[1].cumtime, reverse=True)[:25] if stats else ())
    ]
    states = [profile.status() for profile in profiles]
    passed = all(state.maintenance_state == "ready" for state in states) and company_error is None
    emit_report(
        {
            "schema_version": "history_sqlite_performance",
            "status": "passed" if passed else "degraded",
            "summary": {
                "read_only": True,
                "elapsed_seconds": round(time.monotonic() - total_started, 3),
                "history_seconds": history_seconds,
                "reference_seconds": reference_seconds,
                "model_seconds": model_seconds,
                "company_seconds": company_seconds,
                "binding_seconds": binding_seconds,
                "company_error": company_error,
                "feature_audit": feature_measurement,
                "company_functions": [
                    {"function": name, "module": Path(value.file_name).name, "calls": value.ncalls,
                     "self_seconds": value.tottime, "cumulative_seconds": value.cumtime}
                    for name, value in (sorted(pstats.Stats(company_profiler).get_stats_profile().func_profiles.items(), key=lambda item: item[1].cumtime, reverse=True)[:20] if cpu_profile and company_seconds else ())
                ],
                "model_versions": {profile: CanonicalDigest(model.publications) for profile, model in zip(selected, models, strict=True)},
                "environment": {"python": platform.python_version(), "platform": platform.platform(), "cpu_count": os.cpu_count()},
                "logical_cache_bytes": sum(item.estimated_bytes for sources in cache.status().datasets.values() for item in sources.values()),
                "process_peak_rss_bytes": _peak_rss_bytes(),
                "snapshot_hash": manifest.snapshot_hash if manifest else None,
                "config_version": settings.config_version,
                "sqlite": measurement.result,
                "cpu_profile": cpu_profile,
                "scan_count": owner.scan_count,
                "window_count": owner.window_count,
                "history_phases": {
                    "read_seconds": owner.timings.read_seconds,
                    "alignment_seconds": owner.timings.alignment_seconds,
                    "derivation_seconds": owner.timings.derivation_seconds,
                    "publication_seconds": owner.timings.publication_seconds,
                    "extension_requests": owner.timings.extension_requests,
                    "extension_rows": owner.timings.extension_rows,
                },
                "profiles": [
                    {"sessions": profile.history_sessions, "entries": state.entries, "reason": state.maintenance_reason}
                    for profile, state in zip(profiles, states, strict=True)
                ],
                "functions": functions,
            },
        }
    )
    return 0


def _measure_features(
    config: Path,
    selected: tuple[ScoringProfileId, ...],
    profiles: tuple[PublishedHistoryCache, ...],
    research: PublishedCompanyResearch,
    references: PublishedReferences,
    cache: BoundedLruCache[object],
    observed: datetime,
) -> dict[str, object]:
    """Keep real fixed inputs/cache ownership; synthetic quotes imply no live acceptance."""
    settings = LoadRuntimeSettings(config)
    entries = profiles[0].entries()
    industries = references.model_industries(tuple(entries))
    quotes = tuple(
        MarketQuote(
            code=code, name=code, price=entry.bars[-1].close, previous_close=entry.bars[-1].close,
            open_price=entry.bars[-1].open_price, high=entry.bars[-1].high, low=entry.bars[-1].low,
            pct_change=0.0, change_5m=0.0, speed=0.0, volume_ratio=1.0, turnover_rate=entry.bars[-1].turnover_rate,
            amount=entry.bars[-1].amount, amplitude=2.0, market_cap=1e10,
            industry=industries[code].industry_id if code in industries else "",
            source="diagnostic_synthetic", source_time=observed, received_time=observed,
            data_version="diagnostic_synthetic", volume=entry.bars[-1].volume,
        )
        for code, entry in entries.items() if entry.bars
    )
    codes = tuple(quote.code for quote in quotes)
    observations = research.cached(codes, include_structured=True, as_of=observed)
    barrier = threading.Barrier(len(selected))

    def measure(
        profile_id: ScoringProfileId,
        history: PublishedHistoryCache,
        *,
        profile_cpu: bool = False,
        factor_cache: BoundedLruCache[object] | None = cache,
    ) -> dict[str, object]:
        strategy = LoadStrategySettings(settings.strategy_config_path, scoring_profile=profile_id)
        training = LoadRuntimeTrainingSettings(settings.project_root, profile_id)
        builder = FeatureBuilder(
            strategy.news_signal, strategy.tomorrow_tail_signal, strategy.market_regime,
            strategy.long_research, strategy.feature_component_weights,
            model_momentum_horizons=training.momentum_horizons, cache=factor_cache,
        )
        histories = history.cached(codes, observed_at=observed)
        summaries = history.summaries(histories, observed)
        profiler = cProfile.Profile() if profile_cpu else None
        if not profile_cpu:
            barrier.wait(timeout=30)
        started = time.monotonic()
        if profiler is not None:
            profiler.enable()
        try:
            features = builder.build(quotes, histories, observed, research_observations=observations, history_summaries=summaries)
        finally:
            if profiler is not None:
                profiler.disable()
        elapsed = time.monotonic() - started
        stats = pstats.Stats(profiler).get_stats_profile() if profiler else None
        return {
            "profile": profile_id, "rows": len(features), "elapsed_seconds": elapsed,
            "functions": [
                {"function": name, "module": Path(value.file_name).name, "calls": value.ncalls,
                 "self_seconds": value.tottime, "cumulative_seconds": value.cumtime}
                for name, value in (sorted(stats.func_profiles.items(), key=lambda item: item[1].cumtime, reverse=True)[:35] if stats else ())
            ],
        }

    cold = measure(selected[-1], profiles[-1], profile_cpu=True, factor_cache=BoundedLruCache(settings.market_data.cache_policy))
    uncached = measure(selected[-1], profiles[-1], profile_cpu=True, factor_cache=None)
    with ThreadPoolExecutor(max_workers=len(profiles), thread_name_prefix="feature-audit") as pool:
        futures = [pool.submit(measure, name, profile) for name, profile in zip(selected, profiles, strict=True)]
        measurements = [future.result(timeout=120) for future in futures]
    return {
        "quote_source": "synthetic", "concurrent": True, "profiles": measurements,
        "cold_serial_cpu_profile": cold,
        "uncached_serial_cpu_profile": uncached,
        "serial_cpu_profile": measure(selected[-1], profiles[-1], profile_cpu=True),
    }


def _measure_resume_reads(root: Path, count: int) -> int:
    if not 1 <= count <= 8:
        raise ValueError("history resume sample count must be within 1..8")
    with (
        SqliteSession(root / "catalog.sqlite3", SqliteConnectionOptions(read_only=True)) as connection,
        SqliteTransaction(connection),
    ):
        sequence = int(connection.run("SELECT sequence FROM download_batch").require_one()[0])
        tasks = connection.run(
            "SELECT code,dates_json FROM download_tasks WHERE tencent_done=0 OR failure IS NOT NULL "
            "ORDER BY tencent_done,code LIMIT ?",
            (count,),
        ).fetchall()
    measurements: list[dict[str, object]] = []
    started = time.monotonic()
    prices = HistoryDirectPrices(
        root, sequence, None, HistorySqliteActivity(lambda: None, lambda: time.monotonic() - started >= 60)
    )
    status = "passed"
    try:
        for code, payload in tasks:
            dates = tuple(date.fromisoformat(value) for value in json.loads(payload))
            before = time.monotonic()
            cells = prices.read_for_download(code, dates)
            measurements.append(
                {
                    "requested_cells": len(dates),
                    "existing_cells": len(cells),
                    "obtained_cells": sum(cell.obtained for cell in cells),
                    "read_ms": round((time.monotonic() - before) * 1000, 3),
                    "content_hash": CanonicalDigest(cells),
                }
            )
    except KeyboardInterrupt:
        if time.monotonic() - started < 60:
            raise
        status = "failed"
    emit_report(
        {
            "schema_version": "history_sqlite_performance",
            "status": status,
            "summary": {
                "resume_reads": measurements,
                "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                "reason": "local_read_deadline" if status == "failed" else None,
            },
        }
    )
    return int(status == "failed")


def inspect_history_space(root: Path) -> dict[str, object]:
    """Count inode ownership without reading database payloads or deleting anything."""
    groups: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    allocated: dict[tuple[int, int], int] = {}
    links: dict[tuple[int, int], set[str]] = defaultdict(set)
    unsafe = 0
    for path in root.rglob("*"):
        if path.is_symlink():
            unsafe += 1
            continue
        if not path.is_file():
            continue
        status = path.stat()
        name = path.name
        category = (
            "pending"
            if name.endswith(".pending.sqlite3")
            else "seal"
            if name.endswith(".seal.sqlite3")
            else "rollback"
            if ".rollback.sqlite3" in name
            else "sidecar"
            if name.endswith(("-wal", "-shm", "-journal"))
            else "published"
            if name.startswith("prices_") and name.endswith(".sqlite3")
            else "control_or_stage"
        )
        groups[category][0] += 1
        groups[category][1] += status.st_size
        inode = (status.st_dev, status.st_ino)
        allocated[inode] = status.st_blocks * 512 if hasattr(status, "st_blocks") else status.st_size
        links[inode].add(category)
    return {
        "groups": {name: {"files": counts[0], "logical_bytes": counts[1]} for name, counts in sorted(groups.items())},
        "unique_allocated_bytes": sum(allocated.values()),
        "shared_between_groups_bytes": sum(allocated[key] for key, owners in links.items() if len(owners) > 1),
        "unsafe_symlinks": unsafe,
        "cleanup_performed": False,
        "cleanup_requires_task_and_snapshot_ownership": True,
    }


def inspect_history_sqlite(root: Path, page_sample_count: int = 1) -> _PhysicalSummary:
    state = HistoryControlIndex(root / "catalog.sqlite3").load_state()
    current_snapshot = state.published_snapshot
    if current_snapshot is None:
        raise RuntimeError("history SQLite has no published snapshot")
    total_bytes = 0
    page_count = 0
    free_pages = 0
    overflow_bytes = 0
    index_bytes = 0
    page_sizes: set[int] = set()
    physical_paths = tuple(sorted({item.relative_path for item in current_snapshot.partitions}))
    sampled = _sampled_positions(len(physical_paths), page_sample_count)
    for position, relative_path in enumerate(physical_paths):
        path = root / relative_path
        if not path.is_file() or path.is_symlink():
            raise RuntimeError("history SQLite partition is unavailable")
        total_bytes += path.stat().st_size
        with SqliteSession(path, SqliteConnectionOptions(read_only=True, timeout_seconds=5.0)) as connection:
            page_size = int(connection.run("PRAGMA page_size").require_one()[0])
            page_sizes.add(page_size)
            page_count += int(connection.run("PRAGMA page_count").require_one()[0])
            free_pages += int(connection.run("PRAGMA freelist_count").require_one()[0])
            if position in sampled:
                overflow_bytes += int(
                    connection.run(
                        "SELECT COALESCE(SUM(pgsize), 0) FROM dbstat WHERE pagetype='overflow'"
                    ).require_one()[0]
                )
                index_bytes += int(
                    connection.run(
                        "SELECT COALESCE(SUM(pgsize), 0) FROM dbstat "
                        "WHERE name IN ('history_price_code_date_idx', 'history_price_date_board_code_idx', "
                        "'history_price_observation_code_date_idx')"
                    ).require_one()[0]
                )
    return _PhysicalSummary(
        len(physical_paths),
        total_bytes,
        page_count,
        free_pages,
        overflow_bytes,
        index_bytes,
        tuple(sorted(page_sizes)),
        len(sampled),
    )


def inspect_history_sqlite_performance(
    root: Path,
    page_sample_count: int = 1,
    query_rounds: int = 3,
    revision_write_sample_count: int = 512,
) -> _HistorySQLitePerformanceReport:
    control = HistoryControlIndex(root / "catalog.sqlite3").load_state()
    current_snapshot = control.published_snapshot
    if current_snapshot is None:
        raise RuntimeError("history SQLite has no published snapshot")
    calendar_identity = next(
        (item for item in control.calendars if item.content_hash == current_snapshot.calendar_hash), None
    )
    if calendar_identity is None:
        raise RuntimeError("history SQLite calendar is unavailable")
    physical = inspect_history_sqlite(root, page_sample_count)
    queries, sample = _measure_queries(
        root,
        current_snapshot,
        calendar_identity.open_dates,
        query_rounds,
        revision_write_sample_count,
    )
    revision_write = _measure_revision_write(sample)
    return _HistorySQLitePerformanceReport(physical, queries, revision_write, _peak_rss_bytes())


def _measure_queries(
    root: Path,
    current_snapshot: HistorySnapshot,
    open_dates: tuple[date, ...],
    rounds: int,
    revision_sample_count: int,
) -> tuple[tuple[_QueryMeasurement, ...], tuple[HistoryRevision, ...]]:
    reference = current_snapshot.partitions[-1]
    path, partition = _price_partition(root, reference)
    year, month = _partition_identity(reference)
    month_start = date(year, month, 1)
    month_end = min(current_snapshot.data_cutoff, date(year, month, calendar.monthrange(year, month)[1]))
    with SqliteSession(path, SqliteConnectionOptions(read_only=True)) as connection:
        seed = connection.run(
            "SELECT trade_date, code, board FROM daily_price_revision ORDER BY trade_date DESC, code LIMIT 1"
        ).fetchone()
    if seed is None:
        raise RuntimeError("history SQLite query sample is empty")
    sample_day, code, board = DecodeDate(seed[0]), str(seed[1]), str(seed[2])
    code_dates = tuple(day for day in open_dates if day <= sample_day)[-61:]
    if len(code_dates) != 61:
        raise RuntimeError("history SQLite code query window is unavailable")
    code_paths = tuple(
        root / item.relative_path
        for item in current_snapshot.partitions
        if code_dates[0] <= _partition_end(item) and _partition_start(item) <= code_dates[-1]
    )
    trace: list[str] = []
    traced = HistoryPricePartition(
        path,
        year,
        month,
        statement_trace=trace.append,
    )
    workloads: tuple[tuple[str, Callable[[], int]], ...] = (
        (
            "startup_month_by_code",
            lambda: sum(
                1
                for _ in partition.iter_range_by_code(
                    month_start, month_end, snapshot_sequence=current_snapshot.sequence
                )
            ),
        ),
        (
            "latest_month",
            lambda: sum(
                1 for _ in partition.iter_range(month_start, month_end, snapshot_sequence=current_snapshot.sequence)
            ),
        ),
        (
            "single_code_window",
            lambda: _count_code_window(root, current_snapshot, code_dates[0], code_dates[-1], code),
        ),
        (
            "single_day_board",
            lambda: len(partition.read_day(sample_day, snapshot_sequence=current_snapshot.sequence, board=board)),
        ),
        (
            "training_projection",
            lambda: sum(
                1
                for _ in partition.iter_training_rows(
                    month_start, month_end, snapshot_sequence=current_snapshot.sequence
                )
            ),
        ),
        ("published_price_gaps", lambda: len(partition.missing_price_dates(current_snapshot.sequence))),
    )
    traced_workloads: tuple[Callable[[], int], ...] = (
        lambda: sum(
            1 for _ in traced.iter_range_by_code(month_start, month_end, snapshot_sequence=current_snapshot.sequence)
        ),
        lambda: sum(1 for _ in traced.iter_range(month_start, month_end, snapshot_sequence=current_snapshot.sequence)),
        lambda: sum(
            1 for _ in traced.iter_range(month_start, month_end, snapshot_sequence=current_snapshot.sequence, code=code)
        ),
        lambda: len(traced.read_day(sample_day, snapshot_sequence=current_snapshot.sequence, board=board)),
        lambda: sum(
            1 for _ in traced.iter_training_rows(month_start, month_end, snapshot_sequence=current_snapshot.sequence)
        ),
        lambda: len(traced.missing_price_dates(current_snapshot.sequence)),
    )
    measurements: list[_QueryMeasurement] = []
    for (name, workload), traced_workload in zip(workloads, traced_workloads, strict=True):
        trace.clear()
        traced_workload()
        statement = next(
            value
            for value in trace
            if value.lstrip().upper().startswith(("WITH LATEST AS", "SELECT RECORDS.TRADE_DATE", "SELECT R.CODE"))
        )
        query_plan = _query_plan(path, statement)
        workload_paths = code_paths if name == "single_code_window" else (path,)
        cold = _timings(workload_paths, workload, rounds, cold=True)
        warm = _timings(workload_paths, workload, rounds, cold=False)
        row_count = workload()
        measurements.append(
            _QueryMeasurement(
                name,
                row_count,
                round(statistics.median(cold), 3),
                round(statistics.median(warm), 3),
                any("TEMP B-TREE" in item.upper() for item in query_plan),
                query_plan,
            )
        )
    sample = tuple(
        islice(
            partition.iter_range(month_start, month_end, snapshot_sequence=current_snapshot.sequence),
            revision_sample_count,
        )
    )
    return tuple(measurements), sample


def _timings(
    paths: tuple[Path, ...],
    workload: Callable[[], int],
    rounds: int,
    *,
    cold: bool,
) -> tuple[float, ...]:
    values: list[float] = []
    for _ in range(rounds):
        if cold:
            for path in paths:
                _drop_file_cache(path)
        started = time.perf_counter()
        workload()
        values.append((time.perf_counter() - started) * 1_000.0)
    return tuple(values)


def _query_plan(path: Path, statement: str) -> tuple[str, ...]:
    with SqliteSession(path, SqliteConnectionOptions(read_only=True)) as connection:
        rows = connection.run(f"EXPLAIN QUERY PLAN {statement}")
        return tuple(str(row[3]) for row in rows)


def _measure_revision_write(sample: tuple[HistoryRevision, ...]) -> _RevisionWriteMeasurement:
    if not sample:
        raise RuntimeError("history SQLite revision write sample is empty")
    first = sample[0]
    year = first.trade_date.year
    month = first.trade_date.month
    statements: list[str] = []
    with tempfile.TemporaryDirectory(prefix="trader-history-write-probe-") as directory:
        path = Path(directory) / f"prices_{year:04d}.sqlite3"
        partition = HistoryPricePartition(path, year, month)
        partition.initialize()
        before = _database_bytes(path)
        traced = HistoryPricePartition(path, year, month, statement_trace=statements.append)
        started = time.perf_counter()
        traced.save_revisions(sample)
        elapsed = time.perf_counter() - started
        growth = _database_bytes(path) - before
        seal_started = time.perf_counter()
        partition.seal()
        seal_ms = (time.perf_counter() - seal_started) * 1000.0
    statement_count = sum(not value.lstrip().upper().startswith("PRAGMA") for value in statements)
    transaction_count = sum(value.lstrip().upper().startswith("BEGIN IMMEDIATE") for value in statements)
    return _RevisionWriteMeasurement(
        len(sample),
        statement_count,
        transaction_count,
        round(elapsed * 1_000.0, 3),
        round(len(sample) / elapsed, 3),
        growth,
        round(seal_ms, 3),
    )


def _price_partition(
    root: Path,
    reference: HistorySnapshotPartition,
) -> tuple[Path, HistoryPricePartition]:
    year, month = _partition_identity(reference)
    path = root / reference.relative_path
    return path, HistoryPricePartition(path, year, month)


def _partition_identity(reference: HistorySnapshotPartition) -> tuple[int, int]:
    return reference.calendar_year, reference.calendar_month


def _partition_start(reference: HistorySnapshotPartition) -> date:
    year, month = _partition_identity(reference)
    return date(year, month, 1)


def _partition_end(reference: HistorySnapshotPartition) -> date:
    year, month = _partition_identity(reference)
    return date(year, month, calendar.monthrange(year, month)[1])


def _count_code_window(
    root: Path,
    current_snapshot: HistorySnapshot,
    start: date,
    end: date,
    code: str,
) -> int:
    total = 0
    for reference in current_snapshot.partitions:
        if end < _partition_start(reference) or _partition_end(reference) < start:
            continue
        _path, partition = _price_partition(root, reference)
        total += sum(
            1
            for _ in partition.iter_range(
                max(start, _partition_start(reference)),
                min(end, _partition_end(reference)),
                snapshot_sequence=current_snapshot.sequence,
                code=code,
            )
        )
    return total


def _database_bytes(path: Path) -> int:
    return sum(candidate.stat().st_size for candidate in (path, Path(f"{path}-wal")) if candidate.exists())


def _drop_file_cache(path: Path) -> None:
    if not hasattr(os, "posix_fadvise") or not hasattr(os, "POSIX_FADV_DONTNEED"):
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.posix_fadvise(descriptor, 0, 0, os.POSIX_FADV_DONTNEED)
    finally:
        os.close(descriptor)


def _peak_rss_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == "darwin" else value * 1024)


def _sampled_positions(total: int, requested: int) -> frozenset[int]:
    if requested >= total:
        return frozenset(range(total))
    if requested == 1:
        return frozenset((total - 1,))
    return frozenset(round(index * (total - 1) / (requested - 1)) for index in range(requested))


if __name__ == "__main__":
    raise SystemExit(main())
