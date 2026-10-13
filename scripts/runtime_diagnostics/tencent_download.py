"""Measure the actual Tencent qfq updater and compare history tails without publication."""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass, replace
from datetime import date, datetime
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import requests

from trader.download.application.read_published_history import ReadPublishedHistoryUseCase
from trader.download.application.update_qfq import UpdateQfqWindows
from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.download.domain.history_sync import HistorySupplierContext
from trader.download.domain.published_history import PublishedHistorySide, PublishedHistoryWindow
from trader.download.domain.qfq_window import completed_daily_cutoff
from trader.download.infra.exchange_security_universe import load_current_a_share_universe
from trader.download.infra.published_history_archive import SQLitePublishedHistoryArchive
from trader.download.infra.qfq_checkpoint import QfqCheckpoint
from trader.download.infra.qfq_sqlite import SQLiteQfqWindowCache
from trader.download.infra.tencent_qfq_supplier import (
    TencentQfqDependencies,
    TencentQfqOptions,
    TencentQfqSupplier,
    TencentSessionPool,
)
from trader.infra.market_data.providers.exchange_security_master import fetch_sse_listings, fetch_szse_listings
from trader.infra.shutdown import ShutdownDeadline
from trader.infra.workers import BoundedExecutor

from .reporting import emit_report


@dataclass(frozen=True)
class _SampleSupplier:
    context: HistorySupplierContext
    supplier: TencentQfqSupplier

    def load_qfq_context(self, _as_of: date, _sessions: int) -> HistorySupplierContext:
        return self.context

    def fetch_window(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> PublishedHistoryWindow:
        return self.supplier.fetch_window(security, dates)


def _benchmark(
    supplier: TencentQfqSupplier, context: HistorySupplierContext, size: int, workers: int, observed_at: datetime
) -> dict[str, object]:
    eligible = tuple(item for item in context.universe if len(context.calendar.expected_dates(item)) == 251)
    if len(eligible) < size:
        raise ValueError("benchmark universe is insufficient")
    # Spread the sample across the official ordered universe, rather than measuring one exchange only.
    subset = tuple(eligible[index * len(eligible) // size] for index in range(size))
    sample_supplier = _SampleSupplier(replace(context, universe=subset), supplier)
    pool = BoundedExecutor(worker_count=workers, queue_capacity=0, thread_name_prefix="tencent-diagnostic")
    pool.start()
    try:
        with TemporaryDirectory(prefix="trader-tencent-qfq-") as scratch:
            root = Path(scratch)
            failures: Counter[str] = Counter()

            def feedback(message: str) -> None:
                if " | qfq_download 股票进度 | " in message and (
                    " | 结果 待重试 | " in message or " | 结果 失败 | " in message
                ):
                    failures[message.rsplit(" | 原因 ", 1)[-1]] += 1

            updater = UpdateQfqWindows(
                SQLiteQfqWindowCache(root, "v2"),
                SQLiteQfqWindowCache(root, "v3"),
                sample_supplier,
                QfqCheckpoint(root / ".checkpoint.json"),
                lambda: False,
                feedback,
                workers=workers,
                worker_pool=pool,
            )
            started = time.monotonic()
            result = updater.execute(observed_at)
            elapsed = time.monotonic() - started
            paired = sum(
                len(updater.v2.read_code(item.code).cells) == 251 and len(updater.v3.read_code(item.code).cells) == 61
                for item in subset
            )
            initial_failures = dict(failures)
            failures.clear()
            resumed = updater.execute(observed_at)
            return {
                "stocks": size,
                "workers": workers,
                "elapsed_seconds": round(elapsed, 4),
                "completed": result.completed_codes,
                "pending": result.pending_codes,
                "validated_v2_v3_pairs": paired,
                "changed_rows": result.changed_rows,
                "restart_skipped": resumed.skipped_codes,
                "restart_changed_files": len(resumed.changed_files),
                "failure_categories": initial_failures,
                "restart_failure_categories": dict(failures),
            }
    finally:
        stopped = pool.stop(wait=True, cancel_futures=True, deadline=ShutdownDeadline.start(30))
        if not stopped.completed:
            raise RuntimeError("diagnostic_worker_shutdown_incomplete")


def _prices(side: PublishedHistorySide | None) -> tuple[float | None, ...] | None:
    if side is None:
        return None
    return side.open_price, side.high_price, side.low_price, side.close_price


def compare_windows(reference: PublishedHistoryWindow, incoming: PublishedHistoryWindow) -> dict[str, int]:
    old = {cell.trade_date: cell for cell in reference.cells}
    comparable = differing = missing = 0
    for cell in incoming.cells:
        baseline = old.get(cell.trade_date)
        if (
            baseline is None
            or baseline.unadjusted is None
            or baseline.qfq is None
            or cell.unadjusted is None
            or cell.qfq is None
        ):
            missing += 1
            continue
        comparable += 1
        if _prices(baseline.unadjusted) != _prices(cell.unadjusted) or _prices(baseline.qfq) != _prices(cell.qfq):
            differing += 1
    return {"compared_rows": comparable, "price_differences": differing, "missing_rows": missing}


def _shadow(
    supplier: TencentQfqSupplier, context: HistorySupplierContext, args: argparse.Namespace
) -> dict[str, object]:
    reader = ReadPublishedHistoryUseCase(SQLitePublishedHistoryArchive(args.history_root))
    manifest = reader.manifest()
    if manifest is None:
        return {"status": "unavailable", "reason": "history_snapshot_unavailable"}
    identities = {item.code: item for item in context.universe}
    codes = tuple(code for code in args.codes if code in manifest.universe_codes and code in identities)[:10]
    if not codes:
        return {"status": "unavailable", "reason": "no_shared_history_codes"}
    totals = {"compared_rows": 0, "price_differences": 0, "missing_rows": 0, "failed_codes": 0}
    for code in codes:
        try:
            windows = reader.read_windows(manifest, (code,), sessions=5)
            if not windows or not windows[0].cells:
                totals["failed_codes"] += 1
                continue
            reference = windows[0]
            dates = tuple(cell.trade_date for cell in reference.cells)
            incoming = supplier.fetch_window(identities[code], dates)
            for key, count in compare_windows(reference, incoming).items():
                totals[key] += count
        except (RuntimeError, OSError, ValueError, sqlite3.Error):
            totals["failed_codes"] += 1
    return {"status": "compared", "sampled_codes": len(codes), **totals}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", nargs="+", type=int, default=(10, 50, 100))
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--timeout-seconds", type=float, default=15)
    parser.add_argument("--tencent-history-host", choices=("proxy", "direct"), default="proxy")
    parser.add_argument("--codes", nargs="+", default=("600519", "000001", "300750", "688981", "601318"))
    parser.add_argument("--history-root", type=Path, required=True)
    parser.add_argument("--whole-window", action="store_true", help="probe 2000-row responses without active data")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.whole_window:
        return _whole_window_probe(args)
    sessions: tuple[requests.Session, ...] = ()
    try:
        if len(args.sizes) > 3 or any(not 1 <= size <= 100 for size in args.sizes) or not 1 <= args.workers <= 12:
            raise ValueError("Tencent diagnostic sizes/workers are out of bounds")
        observed_at = datetime.now(ZoneInfo("Asia/Shanghai"))
        sessions = tuple(requests.Session() for _index in range(args.workers))
        session_pool = TencentSessionPool(sessions)
        supplier = TencentQfqSupplier(
            TencentQfqDependencies(
                session_pool.borrow,
                partial(
                    load_current_a_share_universe,
                    partial(fetch_sse_listings, get=requests.get),
                    partial(fetch_szse_listings, get=requests.get),
                    args.timeout_seconds,
                ),
                lambda: False,
            ),
            TencentQfqOptions(timeout_seconds=args.timeout_seconds, history_host=args.tencent_history_host),
        )
        started = time.monotonic()
        context = supplier.load_qfq_context(completed_daily_cutoff(observed_at), 251)
        context_elapsed = time.monotonic() - started
        experiments = [_benchmark(supplier, context, size, args.workers, observed_at) for size in args.sizes]
        complete = all(
            item["completed"] == item["stocks"]
            and item["pending"] == 0
            and item["validated_v2_v3_pairs"] == item["stocks"]
            and item["restart_skipped"] == item["stocks"]
            and item["restart_changed_files"] == 0
            for item in experiments
        )
        report = {
            "schema_version": "tencent-download-diagnostic",
            "status": "passed" if complete else "degraded",
            "summary": {
                "context_elapsed_seconds": round(context_elapsed, 4),
                "universe_rows": len(context.universe),
                "calendar_cutoff": context.calendar.open_dates[-1].isoformat(),
                "experiments": experiments,
                "history_shadow": _shadow(supplier, context, args),
                "history_cutover_eligible": False,
                "history_blockers": [
                    "2000_session_contract_unverified",
                    "historical_st_and_status_unavailable",
                    "cross_source_adjustment_basis_unverified",
                ],
            },
        }
    except (OSError, RuntimeError, ValueError, requests.RequestException) as exc:
        report = {"schema_version": "tencent-download-diagnostic", "status": "failed", "error": type(exc).__name__}
    finally:
        for session in sessions:
            session.close()
    emit_report(report)
    return 1 if report["status"] == "failed" else 0


def _whole_window_probe(args: argparse.Namespace) -> int:
    if not 1 <= len(args.codes) <= 3:
        raise ValueError("whole-window probe permits 1..3 securities")
    end = completed_daily_cutoff(datetime.now(ZoneInfo("Asia/Shanghai")))
    results: list[dict[str, object]] = []
    with requests.Session() as session:
        observations: list[dict[str, object]] = []
        session.hooks["response"].append(partial(_observe_history_response, observations))
        pool = TencentSessionPool((session,))
        supplier = TencentQfqSupplier(
            TencentQfqDependencies(pool.borrow, lambda day: (), lambda: False),
            TencentQfqOptions(timeout_seconds=args.timeout_seconds, retries=0, history_host=args.tencent_history_host),
        )
        try:
            dates = supplier.load_calendar(end, 2000).open_dates
        except (OSError, RuntimeError, ValueError, requests.RequestException) as exc:
            known_reasons = {
                "tencent_qfq_request_failed",
                "tencent_daily_response_rejected",
                "tencent_daily_stock_data_missing",
                "tencent_daily_qualified_rows_missing",
                "tencent_daily_row_invalid",
                "Tencent exchange calendar is incomplete or invalid",
            }
            emit_report(
                {
                    "schema_version": "tencent-whole-window-probe",
                    "status": "failed",
                    "summary": {
                        "error": type(exc).__name__,
                        "stage": "calendar",
                        "reason": str(exc) if str(exc) in known_reasons else "calendar_probe_failed",
                        "requests": observations,
                    },
                }
            )
            return 1
        for code in args.codes:
            observations.clear()
            security = BaoStockSecurity(code, "capability probe", "main", date(1990, 1, 1), None, "diagnostic")
            started = time.monotonic()
            try:
                window = supplier.fetch_window(security, dates, request_count=2000, allow_interior_padding=True)
                paired = tuple(cell.trade_date for cell in window.cells if cell.status == "complete")
                results.append(
                    {
                        "raw_rows": sum(cell.unadjusted is not None for cell in window.cells),
                        "qfq_rows": sum(cell.qfq is not None for cell in window.cells),
                        "paired_rows": len(paired),
                        "expected_sessions": len(dates),
                        "missing_sessions": len(dates) - len(paired),
                        "first_date": paired[0].isoformat() if paired else None,
                        "last_date": paired[-1].isoformat() if paired else None,
                        "seconds": round(time.monotonic() - started, 3),
                        "requests": list(observations),
                    }
                )
            except (OSError, RuntimeError, ValueError, requests.RequestException) as exc:
                results.append({"failure": type(exc).__name__, "seconds": round(time.monotonic() - started, 3)})
    failed = any("failure" in item for item in results)
    passed = all(item.get("paired_rows") == 2000 for item in results)
    emit_report(
        {
            "schema_version": "tencent-whole-window-probe",
            "status": "failed" if failed else "passed" if passed else "degraded",
            "summary": {
                "requested_rows_per_side": 2000,
                "calendar_coverage_verified": True,
                "security_listing_dates_verified": False,
                "samples": results,
            },
        }
    )
    return 1 if failed else 0


def _observe_history_response(
    observations: list[dict[str, object]], response: requests.Response, **_kwargs: object
) -> None:
    """Report only bounded date/count metadata, never prices or vendor payloads."""
    if len(observations) >= 64:
        return
    url = urlsplit(response.url)
    param = parse_qs(url.query).get("param", [""])[0].split(",")
    if len(param) != 6:
        return
    item: dict[str, object] = {
        "host": url.hostname,
        "start": param[2],
        "end": param[3],
        "mode": param[5],
        "http_status": response.status_code,
    }
    try:
        text = response.text
        marker = text.find("={")
        payload = json.loads(text[marker + 1 :] if marker >= 0 else text)
        stock = payload["data"][param[0]]
        row_kind = "qfqday" if param[5] == "qfq" and "qfqday" in stock else "day"
        rows = stock.get(row_kind, [])
        if not isinstance(rows, list):
            raise ValueError("invalid response row container")
        item.update(
            rows=len(rows),
            row_kind=row_kind,
            first=date.fromisoformat(rows[0][0]).isoformat() if rows else None,
            last=date.fromisoformat(rows[-1][0]).isoformat() if rows else None,
        )
    except (ValueError, KeyError, TypeError, IndexError, AttributeError):
        item["response_metadata_invalid"] = True
    observations.append(item)


if __name__ == "__main__":
    raise SystemExit(main())
