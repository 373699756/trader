from dataclasses import dataclass
from datetime import date, timedelta

import pytest

from trader.application.research.baostock_daily import BaoStockDailyDownloadService, BaoStockShardContext
from trader.domain.research.baostock_daily import (
    BaoStockCalendar,
    BaoStockCodeBatch,
    BaoStockDailyCell,
    BaoStockDailySide,
    BaoStockDailySpec,
    BaoStockSecurity,
    BaoStockSourceVersions,
)


def _side(code: str, day: date, adjustment: str) -> BaoStockDailySide:
    return BaoStockDailySide(
        code,
        day,
        adjustment,
        10.0,
        10.5,
        9.8,
        10.2,
        100.0,
        1_000.0,
        9.9 if adjustment == "unadjusted" else None,
        3.03 if adjustment == "unadjusted" else None,
        1.2 if adjustment == "unadjusted" else None,
        "trading",
    )


class _Gateway:
    def __init__(self) -> None:
        self.requested: list[str] = []

    def fetch_calendar(self, spec: BaoStockDailySpec) -> BaoStockCalendar:
        start = spec.source_cutoff - timedelta(days=spec.sessions - 1)
        return BaoStockCalendar(tuple(start + timedelta(days=index) for index in range(spec.sessions)))

    def fetch_universe(self, spec: BaoStockDailySpec) -> tuple[BaoStockSecurity, ...]:
        return tuple(
            BaoStockSecurity(code, code, "main", spec.source_cutoff - timedelta(days=100), None, "fixture")
            for code in ("600001", "600002")
        )

    def fetch_code_batch(
        self, spec: BaoStockDailySpec, security: BaoStockSecurity, calendar: BaoStockCalendar
    ) -> BaoStockCodeBatch:
        self.requested.append(security.code)
        cells = tuple(
            BaoStockDailyCell(
                security.code,
                day,
                "complete",
                _side(security.code, day, "unadjusted"),
                _side(security.code, day, "qfq"),
            )
            for day in calendar.expected_dates(security)
        )
        return BaoStockCodeBatch(security.code, cells)

    def source_versions(self) -> BaoStockSourceVersions:
        return BaoStockSourceVersions("0.9.3", "3.14.0", (("pandas", "2.3.0"),))


@dataclass
class _Archive:
    initialized: bool = False
    reject_saves: bool = False

    def __post_init__(self) -> None:
        self.calendar = None
        self.universe = ()
        self.versions = None
        self.complete = frozenset({"600001"})
        self.saved: dict[str, BaoStockCodeBatch] = {}
        self.failures: dict[str, str] = {}

    def context(self, _spec):
        if not self.initialized:
            return None
        return BaoStockShardContext(self.calendar, self.universe, self.versions)

    def initialize(self, _spec, calendar, universe, versions):
        self.initialized = True
        self.calendar = calendar
        self.universe = tuple(universe)
        self.versions = versions

    def completed_codes(self, _spec):
        return self.complete

    def save_batch(self, _spec, batch):
        if self.reject_saves:
            raise RuntimeError("artifact identity conflict")
        self.saved[batch.code] = batch

    def record_failure(self, _spec, code, error_code):
        self.failures[code] = error_code


def test_download_service_resumes_only_non_completed_codes() -> None:
    gateway = _Gateway()
    archive = _Archive()
    result = BaoStockDailyDownloadService(gateway, archive).execute(BaoStockDailySpec(sessions=3))

    assert result.universe_count == 2
    assert result.previously_completed == 1
    assert result.attempted == 1
    assert result.downloaded == 1
    assert result.failed == 0
    assert gateway.requested == ["600002"]
    assert tuple(archive.saved) == ("600002",)


def test_download_service_does_not_misclassify_archive_conflict_as_supplier_failure() -> None:
    gateway = _Gateway()
    archive = _Archive(reject_saves=True)
    archive.complete = frozenset({"600001"})

    with pytest.raises(RuntimeError, match="artifact identity conflict"):
        BaoStockDailyDownloadService(gateway, archive).execute(BaoStockDailySpec(sessions=3))

    assert archive.failures == {}
