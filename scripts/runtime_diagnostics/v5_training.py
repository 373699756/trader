"""Real bounded-industry V5 fit with read-only source and external disposable outputs."""

from __future__ import annotations

from trader.infra.sqlite_policy import SqliteIoPolicy

import argparse
import platform
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime
from datetime import time as daytime
from pathlib import Path
from zoneinfo import ZoneInfo

from trader.download.domain.baostock_daily import (
    BaoStockCalendar,
    BaoStockCodeBatch,
    BaoStockCodeDownload,
    BaoStockDailyFact,
    BaoStockSecurity,
    BaoStockSourceVersions,
)
from trader.download.domain.history_control import HistorySnapshot
from trader.download.domain.history_revision import HistoryRevision
from trader.download.domain.history_sync import HistorySupplierContext
from trader.download.domain.published_history import ProjectHistoryCell, PublishedHistoryWindow
from trader.download.infra.download_settings import LoadHistorySyncConfiguration
from trader.download.infra.history_control_index import HistoryControlIndex
from trader.download.infra.history_prices import HistoryPricePartition
from trader.download.infra.history_reference_index import ReadBoundHistoryReference
from trader.download.infra.history_snapshot_reader import HistorySnapshotReader
from trader.download.infra.history_sync_runner import RunHistorySync
from trader.download.infra.reference_index import SQLiteReferenceIndex
from trader.infra.settings.data_paths import DataPaths
from trader.infra.storage_probe import StorageProbe
from trader.recommendation.infra.scoring.profile_factory import LoadScoringProfile
from trader.training.infra.engine import ProfileTrainingRequest, RunProfileTraining
from trader.training.infra.profile.v5.contracts import V5_TRAINING_PROFILE
from trader.training.infra.profile.v5.network import ConfigureTcnRuntime
from trader.training.infra.profile.v5.settings import TcnFittingParameters
from trader.training.infra.training_settings import LoadProfileTrainingSettings

from .reporting import emit_report


class _FrozenSubsetSupplier:
    def __init__(self, source: Path, snapshot: HistorySnapshot, context: HistorySupplierContext, *, sqlite_policy: SqliteIoPolicy) -> None:
        self._sqlite_policy = sqlite_policy
        self._context = context
        if len(context.universe) > 40 or len(context.calendar.open_dates) > 2000:
            raise ValueError("V5 isolated export exceeds its bounded input")
        index = HistorySnapshotReader(source, sqlite_policy=self._sqlite_policy).reference_index(snapshot)
        grouped: dict[str, list[HistoryRevision]] = {item.code: [] for item in context.universe}
        for reference in snapshot.partitions:
            dates = tuple(
                day
                for day in context.calendar.open_dates
                if (day.year, day.month) == (reference.calendar_year, reference.calendar_month)
            )
            if not dates:
                continue
            partition = HistoryPricePartition(
                source / reference.relative_path, reference.calendar_year, reference.calendar_month, sqlite_policy=self._sqlite_policy
            )
            keys = tuple((code, day) for day in dates for code in sorted(grouped))
            for row in partition.read_price_keys(keys, reference.sequence):
                grouped[row.code].append(index.apply(row))
        self._revisions = {code: tuple(sorted(rows, key=lambda row: row.trade_date)) for code, rows in grouped.items()}

    def load_context(self, _as_of: date, _sessions: int) -> HistorySupplierContext:
        return self._context

    def fetch_tencent_window(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> PublishedHistoryWindow:
        rows = self._rows(security.code, dates)
        return PublishedHistoryWindow(security.code, tuple(ProjectHistoryCell(row.cell) for row in rows))

    def fetch_baostock_prices(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> BaoStockCodeDownload:
        rows = self._rows(security.code, dates)
        if any(row.is_st is None for row in rows):
            raise ValueError("frozen source is missing ST facts")
        return BaoStockCodeDownload(
            BaoStockCodeBatch(security.code, tuple(row.cell for row in rows)),
            tuple(BaoStockDailyFact(row.code, row.trade_date, row.is_st) for row in rows if row.is_st is not None),
        )

    def fetch_baostock_window(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> PublishedHistoryWindow:
        raise RuntimeError("isolated_frozen_source_has_no_network_repair")

    def _rows(self, code: str, dates: tuple[date, ...]) -> tuple[HistoryRevision, ...]:
        allowed = frozenset(dates)
        return tuple(row for row in self._revisions[code] if row.trade_date in allowed)


def _probe(source: Path, work: Path, *, sqlite_policy: SqliteIoPolicy) -> dict[str, object]:
    import resource

    project = Path(__file__).resolve().parents[2]
    source, work = source.resolve(), work.resolve()
    if work.is_relative_to(project) or work == source or work.is_relative_to(source) or source.is_relative_to(work):
        raise ValueError("V5 probe output must be outside the repository and source")
    if work.exists():
        raise ValueError("V5 probe output directory must not exist")
    state = HistoryControlIndex(source / "catalog.sqlite3", sqlite_policy=sqlite_policy).load_published_state()
    if state is None:
        raise ValueError("V5 probe requires a published history")
    published = HistoryControlIndex(source / "catalog.sqlite3", sqlite_policy=sqlite_policy).read_universe_reference(state.universe.content_hash)
    reference = ReadBoundHistoryReference(source, state.source.supplier_contract, sqlite_policy=sqlite_policy)
    groups: dict[str, set[str]] = defaultdict(set)
    for interval in reference.industry_intervals:
        if interval.effective_to is None:
            groups[interval.industry].add(interval.code)
    eligible = {item.code: item for item in state.universe.securities}
    industry, members = max(groups.items(), key=lambda pair: len(pair[1]))
    selected = sorted(
        (eligible[code] for code in members if code in eligible), key=lambda item: (item.listed_on, item.code)
    )[:40]
    if len(selected) < 30:
        raise ValueError("V5 probe requires at least thirty securities in one industry")
    universe = tuple(
        BaoStockSecurity(item.code, item.name, item.board, item.listed_on, item.delisted_on, "bounded_v5_diagnostic")
        for item in selected
    )
    settings = LoadProfileTrainingSettings(project, "v5")
    if not isinstance(settings.fitting, TcnFittingParameters):
        raise ValueError("V5 diagnostic requires TCN parameters")
    fitting = replace(settings.fitting, epochs=2, early_stopping_rounds=2)
    settings = replace(settings, fitting=fitting)
    work.mkdir(parents=True)
    history_root = work / "history"
    codes = {item.code for item in universe}
    context = HistorySupplierContext(
        BaoStockCalendar(state.calendar.open_dates),
        universe,
        BaoStockSourceVersions("bounded_frozen_v5_export", platform.python_version(), ()),
        tuple(item for item in reference.industry_intervals if item.code in codes),
        tuple(item for item in reference.st_evidence if item.code in codes),
    )
    configuration = replace(
        LoadHistorySyncConfiguration(project, DataPaths(work)),
        history_root=history_root,
        history_workers=1,
        minimum_free_bytes=0,
    )
    subset = replace(published, official_universe=universe, st_evidence=context.st_evidence)
    SQLiteReferenceIndex(configuration.reference_root / "company_st.sqlite3", sqlite_policy=sqlite_policy).publish(subset, subset.st_evidence)
    context = replace(context, reference_id=subset.identity)
    export_started = time.monotonic()
    with ThreadPoolExecutor(max_workers=1) as workers:
        exported = RunHistorySync(
            configuration,
            _FrozenSubsetSupplier(source, state.snapshot, context, sqlite_policy=sqlite_policy),
            worker_pool=workers,
            storage_kind=StorageProbe().inspect(configuration.history_root).kind,
            clock=lambda: datetime.combine(state.snapshot.data_cutoff, daytime(20, 30), ZoneInfo("Asia/Shanghai")), sqlite_policy=sqlite_policy,
        )
    if exported.snapshot_hash is None:
        raise ValueError("V5 isolated source export did not publish")
    export_seconds = time.monotonic() - export_started
    ConfigureTcnRuntime(training=True, compute_threads=fitting.compute_threads)
    started = time.monotonic()
    result = RunProfileTraining(
        ProfileTrainingRequest(
            history_root,
            work / "models",
            V5_TRAINING_PROFILE,
            V5_TRAINING_PROFILE.heads,
            settings,
            sample_root=work / "samples",
        ), sqlite_policy=sqlite_policy
    )
    if result.status == "engineering_ready":
        loaded = LoadScoringProfile("v5", training_root=work / "models")
        reloaded = len(loaded.heads) == 2
    else:
        reloaded = False
    after = HistoryControlIndex(source / "catalog.sqlite3", sqlite_policy=sqlite_policy).load_published_state()
    return {
        "schema_version": "v5-training-diagnostic",
        "status": "passed" if reloaded and after and after.snapshot == state.snapshot else "degraded",
        "summary": {
            "input_scope": "bounded_real_industry_engineering_probe",
            "source_data_writes": 0,
            "source_verification": "published_identity_and_selected_revision_hashes",
            "network_calls": 0,
            "code_count": len(selected),
            "industry": industry,
            "epochs": 2,
            "minimum_industry_training_rows": settings.fitting.minimum_industry_training_rows,
            "seconds": round(time.monotonic() - started, 3),
            "isolated_export_seconds": round(export_seconds, 3),
            "peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
            "reloaded_dual_heads": reloaded,
            "source_identity_unchanged": bool(after and after.snapshot == state.snapshot),
            "heads": [
                {
                    "strategy": head.strategy.value,
                    "status": head.status,
                    "training_rows": head.training_rows,
                    "validation_rows": head.validation_rows,
                    "industry_count": head.industry_count,
                    "failure_reasons": list(head.failure_reasons),
                }
                for head in result.heads
            ],
            "production_authority": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history-root", type=Path)
    parser.add_argument("--work-root", type=Path)
    parser.add_argument("--kernel-probe", action="store_true")
    args = parser.parse_args()
    from trader.bootstrap import BuildSqlitePolicy
    from trader.infra.settings import LoadRuntimeSettings
    sqlite_policy = BuildSqlitePolicy(LoadRuntimeSettings(Path(__file__).resolve().parents[2] / "config/runtime.json"))
    try:
        if args.kernel_probe:
            from .v5_kernels import ProbeV5Kernels

            report = ProbeV5Kernels()
        else:
            if args.history_root is None or args.work_root is None:
                raise ValueError("V5 training probe requires history and output directories")
            report = _probe(args.history_root, args.work_root, sqlite_policy=sqlite_policy)
    except (OSError, RuntimeError, ValueError, AssertionError) as exc:
        report = {
            "schema_version": "v5-training-diagnostic",
            "status": "failed",
            "summary": {"failure": type(exc).__name__},
        }
    emit_report(report)
    return 1 if report["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
