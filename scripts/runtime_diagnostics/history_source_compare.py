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

import requests

from trader.download.domain.baostock_daily import (
    BaoStockCalendar,
    BaoStockCodeDownload,
    BaoStockSecurity,
    BaoStockSourceVersions,
)
from trader.download.domain.history_sync import HistorySupplierContext
from trader.download.domain.published_history import PublishedHistoryWindow, project_history_cell
from trader.download.infra.baostock_sync_supplier import BaoStockHistorySupplier, BaoStockRequestPolicy
from trader.download.infra.download_settings import load_history_sync_configuration
from trader.download.infra.history_control_index import HistoryControlIndex
from trader.download.infra.history_reference_files import read_bound_history_reference
from trader.download.infra.history_supplier_router import fetch_tencent_history_window
from trader.download.infra.history_sync_runner import run_history_sync
from trader.download.infra.tencent_qfq_supplier import (
    TencentQfqDependencies,
    TencentQfqOptions,
    TencentQfqSupplier,
    TencentSessionPool,
)

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
            return fetch_tencent_history_window(self.prices, security, dates, self.context.calendar.open_dates)
        batch = self.baseline.fetch_code(security, dates).batch
        return PublishedHistoryWindow(security.code, tuple(project_history_cell(cell) for cell in batch.cells))

    def fetch_baostock_prices(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> BaoStockCodeDownload:
        return self.baseline.fetch_code(security, dates)


def _compare(root: Path, codes: tuple[str, ...], sessions: int, timeout: float) -> dict[str, object]:
    state = HistoryControlIndex(root / "control.sqlite3").load_published_state()
    if state is None:
        raise ValueError("comparison_requires_published_calendar")
    reference = read_bound_history_reference(root, state.source.supplier_contract)
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
                load_history_sync_configuration(project_root, work / "reference"),
                archive_root=work / "history",
                training_root=work / "train",
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
            sessions_pool = TencentSessionPool(tuple(stack.enter_context(requests.Session()) for _ in range(workers)))
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
            started = time.monotonic()
            result = run_history_sync(
                config,
                _ComparisonSupplier(context, baseline, prices, tencent_first),
                worker_pool=pool,
                clock=lambda: datetime.combine(dates[-1], daytime(20, 30), ZoneInfo("Asia/Shanghai")),
            )
            results.append(
                {
                    "source": name,
                    "seconds": round(time.monotonic() - started, 3),
                    "state": result.state,
                    "reason": result.reason,
                    "unresolved_price_cells": result.unresolved_price_cells,
                    "published": result.active_snapshot_hash is not None,
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
        selected_source = qualified[0]["source"]
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
