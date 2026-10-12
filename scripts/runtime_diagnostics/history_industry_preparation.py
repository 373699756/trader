"""Measure cold/warm two-date industry preparation using isolated query evidence."""

from __future__ import annotations

import argparse
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic

from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.download.domain.history_sync import HistorySyncConfiguration, HistorySyncProgress
from trader.download.infra.baostock_sync_supplier import BaoStockHistorySupplier
from trader.download.infra.history_control_index import HistoryControlIndex
from trader.download.infra.history_industry_cache import HistoryIndustryQueryCache

from .reporting import emit_report


@dataclass
class _Progress:
    values: list[HistorySyncProgress] = field(default_factory=list)

    def publish(self, progress: HistorySyncProgress) -> None:
        self.values.append(progress)


def _measure(root: Path, timeout: float) -> dict[str, object]:
    active = HistoryControlIndex(root / "control.sqlite3").load_published_state()
    if active is None:
        raise ValueError("industry_probe_requires_published_context")
    universe = tuple(
        BaoStockSecurity(item.code, item.name, item.board, item.listed_on, item.delisted_on, "industry-probe")
        for item in active.universe.securities[:3]
    )
    progress = _Progress()
    measurements = []
    intervals = []
    with tempfile.TemporaryDirectory(prefix="trader-industry-preparation-") as temporary:
        cache = HistoryIndustryQueryCache(Path(temporary) / "industry_queries.sqlite3")
        settings = HistorySyncConfiguration(
            supplier_timeout_seconds=timeout, supplier_retries=0, progress_heartbeat_seconds=min(5, timeout)
        )
        for label in ("cold", "warm"):
            progress.values.clear()
            started = monotonic()
            # Include login and prove cache reuse across real supplier processes.
            with BaoStockHistorySupplier(settings, progress=progress) as supplier:
                context = supplier.load_context(
                    active.calendar.open_dates[-1], 2, universe=universe, industry_cache=cache
                )
            completed = [value for value in progress.values if value.cache_hit is not None]
            intervals.append(context.industry_intervals)
            measurements.append(
                {
                    "mode": label,
                    "seconds": round(monotonic() - started, 3),
                    "queried_dates": sum(value.cache_hit is False for value in completed),
                    "cached_dates": sum(value.cache_hit is True for value in completed),
                    "industry_intervals": len(context.industry_intervals),
                }
            )
        passed = (
            measurements[0]["queried_dates"] == 2
            and measurements[1]["queried_dates"] == 0
            and measurements[1]["cached_dates"] == 2
            and bool(intervals[0])
            and intervals[0] == intervals[1]
        )
        return {
            "schema_version": "history-industry-preparation",
            "status": "passed" if passed else "degraded",
            "summary": {
                "sessions": 2,
                "measurements": measurements,
                "identical_intervals": intervals[0] == intervals[1],
                "active_data_modified": False,
            },
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history-root", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=15)
    args = parser.parse_args()
    if not 1 <= args.timeout_seconds <= 45:
        parser.error("attempt timeout must be 1..45 seconds")
    try:
        report = _measure(args.history_root, args.timeout_seconds)
    except (OSError, RuntimeError, ValueError) as exc:
        report = {
            "schema_version": "history-industry-preparation",
            "status": "failed",
            "summary": {"failure": type(exc).__name__},
        }
    emit_report(report)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
