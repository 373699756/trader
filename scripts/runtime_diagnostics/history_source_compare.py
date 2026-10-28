"""Bounded same-input history pipelines; network and temporary output only."""

from __future__ import annotations

import argparse
import platform
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass, replace
from datetime import date, datetime
from datetime import time as daytime
from pathlib import Path
from zoneinfo import ZoneInfo

from trader.bootstrap import OpenExternalHttp
from trader.download.domain.baostock_daily import (
    BaoStockCalendar,
    BaoStockCodeDownload,
    BaoStockSecurity,
    BaoStockSourceVersions,
)
from trader.download.domain.history_sync import HistorySupplierContext
from trader.download.domain.published_history import ProjectHistoryCell, PublishedHistoryWindow
from trader.download.infra.baostock_sync_supplier import BaoStockHistorySupplier, BaoStockRequestPolicy
from trader.download.infra.download_settings import LoadHistorySyncConfiguration
from trader.download.infra.history_control_index import HistoryControlIndex
from trader.download.infra.history_reference_index import ReadBoundHistoryReference
from trader.download.infra.history_supplier_router import FetchTencentHistoryWindow
from trader.download.infra.history_sync_runner import RunHistorySync
from trader.download.infra.reference_index import SQLiteReferenceIndex
from trader.download.infra.tencent_qfq_supplier import (
    TencentQfqDependencies,
    TencentQfqOptions,
    TencentQfqSupplier,
    TencentSessionPool,
)
from trader.infra.settings.data_paths import DataPaths
from trader.infra.storage_probe import StorageProbe

from .reporting import emit_report


@dataclass
class _ComparisonSupplier:
    context: HistorySupplierContext
    baseline: BaoStockHistorySupplier
    prices: TencentQfqSupplier
    tencent_first: bool

    def load_context(self, _as_of: date, _sessions: int) -> HistorySupplierContext:
        return self.context

    def fetch_tencent_window(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> PublishedHistoryWindow:
        if self.tencent_first:
            return FetchTencentHistoryWindow(self.prices, security, dates, self.context.calendar.open_dates)
        batch = self.baseline.fetch_code(security, dates).batch
        return PublishedHistoryWindow(security.code, tuple(ProjectHistoryCell(cell) for cell in batch.cells))

    def fetch_baostock_prices(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> BaoStockCodeDownload:
        return self.baseline.fetch_code(security, dates)


def _compare(root: Path, codes: tuple[str, ...], sessions: int, timeout: float) -> dict[str, object]:
    state = HistoryControlIndex(root / "catalog.sqlite3").load_published_state()
    if state is None:
        raise ValueError("comparison_requires_published_calendar")
    published = HistoryControlIndex(root / "catalog.sqlite3").read_universe_reference(state.universe.content_hash)
    reference = ReadBoundHistoryReference(root, state.source.supplier_contract)
    selected = tuple(item for item in state.universe.securities if item.code in codes)
    if {item.code for item in selected} != set(codes):
        raise ValueError("comparison_codes_outside_filtered_universe")
    dates = state.calendar.open_dates[-sessions:]
    if len(dates) != sessions:
        raise ValueError("comparison_calendar_insufficient")
    universe = tuple(
        BaoStockSecurity(item.code, item.name, item.board, item.listed_on, item.delisted_on, "bounded-comparison")
        for item in selected
    )
    results: list[dict[str, object]] = []
    for tencent_first in (False, True):
        workers = min(8, len(codes)) if tencent_first else 1
        with tempfile.TemporaryDirectory(prefix="trader-source-comparison-") as temporary, ExitStack() as stack:
            work = Path(temporary)
            project_root = Path(__file__).resolve().parents[2]
            config = replace(
                LoadHistorySyncConfiguration(project_root, DataPaths(work / "data")),
                history_root=work / "history",
                sessions=sessions,
                reread_sessions=min(5, sessions),
                history_workers=workers,
                minimum_free_bytes=0,
                supplier_timeout_seconds=timeout,
                supplier_retries=0,
                progress_heartbeat_seconds=min(5, timeout),
            )
            baseline = stack.enter_context(
                BaoStockHistorySupplier(config, price_policy=BaoStockRequestPolicy(timeout, 0))
            )
            pool = stack.enter_context(ThreadPoolExecutor(max_workers=workers))
            sessions_pool = TencentSessionPool(tuple(stack.enter_context(OpenExternalHttp()) for _ in range(workers)))
            prices = TencentQfqSupplier(
                TencentQfqDependencies(sessions_pool.borrow, lambda _day: universe, lambda: False),
                TencentQfqOptions(timeout_seconds=timeout, retries=0),
            )
            name = "tencent_then_baostock_gap" if tencent_first else "baostock"
            context = HistorySupplierContext(
                BaoStockCalendar(dates),
                universe,
                BaoStockSourceVersions(name, platform.python_version(), ()),
                tuple(item for item in reference.industry_intervals if item.code in codes),
                tuple(item for item in reference.st_evidence if item.code in codes),
            )
            subset = replace(published, official_universe=universe, st_evidence=context.st_evidence)
            SQLiteReferenceIndex(config.reference_root / "company_st.sqlite3").publish(subset, subset.st_evidence)
            context = replace(context, reference_id=subset.identity)
            started = time.monotonic()
            result = RunHistorySync(
                config,
                _ComparisonSupplier(context, baseline, prices, tencent_first),
                worker_pool=pool,
                storage_kind=StorageProbe().inspect(config.history_root).kind,
                clock=lambda: datetime.combine(dates[-1], daytime(20, 30), ZoneInfo("Asia/Shanghai")),
            )
            results.append(
                {
                    "source": name,
                    "seconds": round(time.monotonic() - started, 3),
                    "state": result.state,
                    "reason": result.reason,
                    "unresolved_price_cells": result.unresolved_price_cells,
                    "published": result.snapshot_hash is not None,
                    "temporary_bytes": sum(path.stat().st_size for path in work.rglob("*") if path.is_file()),
                }
            )
    qualified = [item for item in results if item["state"] == "completed" and item["unresolved_price_cells"] == 0]
    selected_source = None
    if len(qualified) == 2:
        baseline_time = float(str(results[0]["seconds"]))
        mixed_time = float(str(results[1]["seconds"]))
        selected_source = "tencent_then_baostock_gap" if mixed_time < baseline_time * 0.9 else "baostock"
    elif qualified:
        source = qualified[0]["source"]
        if not isinstance(source, str):
            raise ValueError("comparison_source_invalid")
        selected_source = source
    return {
        "schema_version": "history-source-comparison",
        "status": "passed" if len(qualified) == 2 else "degraded",
        "summary": {
            "code_count": len(codes),
            "sessions": sessions,
            "pipelines": results,
            "suggested_source": selected_source,
            "production_switch": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history-root", type=Path, required=True)
    parser.add_argument("--codes", nargs="+", required=True)
    parser.add_argument("--sessions", type=int, default=251)
    parser.add_argument("--timeout-seconds", type=float, default=20)
    args = parser.parse_args()
    if not 1 <= len(args.codes) <= 10 or not 1 <= args.sessions <= 2000 or not 1 <= args.timeout_seconds <= 45:
        parser.error("comparison requires 1..10 codes, 1..2000 sessions and 1..45 second attempts")
    try:
        report = _compare(args.history_root, tuple(args.codes), args.sessions, args.timeout_seconds)
    except (OSError, RuntimeError, ValueError) as exc:
        emit_report(
            {
                "schema_version": "history-source-comparison",
                "status": "failed",
                "summary": {"failure": type(exc).__name__},
            }
        )
        return 1
    emit_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
