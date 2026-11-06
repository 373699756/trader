from __future__ import annotations
from tests.helpers.shared.configuration import SqlitePolicyFixture
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from trader.download.domain.baostock_daily import (
    BaoStockCalendar,
    BaoStockCodeBatch,
    BaoStockCodeDownload,
    BaoStockDailyCell,
    BaoStockDailyFact,
    BaoStockDailySide,
    BaoStockIndustryInterval,
    BaoStockSecurity,
    BaoStockSourceVersions,
)
from trader.download.domain.history_price_qualification import HISTORY_UNIVERSE_CONTRACT
from trader.download.domain.history_reference import HistoryStEvidence
from trader.download.domain.history_sync import (
    HistorySupplierContext,
    HistorySyncProgress,
)
from trader.download.domain.published_history import ProjectHistoryCell, PublishedHistoryWindow
from trader.download.infra.history_control_index import HistoryControlIndex
from tests.helpers.download.configuration import HistorySyncConfigurationFixture as HistorySyncConfiguration

NOW = datetime(2026, 9, 10, 20, 30, tzinfo=ZoneInfo("Asia/Shanghai"))


def _side(code: str, day: date, adjustment: str, close: float) -> BaoStockDailySide:
    return BaoStockDailySide(
        code,
        day,
        adjustment,  # type: ignore[arg-type]
        close,
        close,
        close,
        close,
        100.0,
        1000.0,
        close if adjustment == "unadjusted" else None,
        0.0 if adjustment == "unadjusted" else None,
        0.01 if adjustment == "unadjusted" else None,
        "trading",
    )


def _download(code: str, dates: tuple[date, ...], qfq_shift: float = 0.0) -> BaoStockCodeDownload:
    cells = tuple(
        BaoStockDailyCell(
            code,
            day,
            "complete",
            _side(code, day, "unadjusted", float(day.day)),
            _side(code, day, "qfq", float(day.day) + qfq_shift),
        )
        for day in dates
    )
    return BaoStockCodeDownload(
        BaoStockCodeBatch(code, cells),
        tuple(BaoStockDailyFact(code, day, False) for day in dates),
    )


@dataclass
class FakeSupplier:
    dates: tuple[date, ...]
    fail_code: str | None = None
    changed_qfq_code: str | None = None
    industry: str | None = None
    calls: list[tuple[str, tuple[date, ...]]] = field(default_factory=list)
    events: list[str] = field(default_factory=list)

    def fetch_baostock_window(self, security, dates):
        raise RuntimeError("fixture_baostock_unavailable")

    def load_context(
        self,
        _as_of: date,
        sessions: int,
        *,
        universe: tuple[BaoStockSecurity, ...] | None = None,
    ) -> HistorySupplierContext:
        selected = self.dates[-sessions:]
        selected_universe = universe or tuple(
            BaoStockSecurity(code, code, "main", date(2000, 1, 1), None, "test") for code in ("600001", "600002")
        )
        intervals = (
            tuple(
                BaoStockIndustryInterval(code, date(2000, 1, 1), None, self.industry, "test")
                for code in (item.code for item in selected_universe)
            )
            if self.industry is not None
            else ()
        )
        return HistorySupplierContext(
            BaoStockCalendar(selected),
            selected_universe,
            BaoStockSourceVersions("test", "3.12", ()),
            intervals,
            tuple(HistoryStEvidence(item.code, _as_of, "clear") for item in selected_universe),
        )

    def fetch_code(
        self,
        security: BaoStockSecurity,
        dates: tuple[date, ...],
    ) -> BaoStockCodeDownload:
        self.calls.append((security.code, dates))
        if security.code == self.fail_code:
            raise RuntimeError("supplier_query_failed")
        shift = 1.0 if security.code == self.changed_qfq_code else 0.0
        return _download(security.code, dates, shift)

    def fetch_tencent_window(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> PublishedHistoryWindow:
        self.events.append("tencent")
        self.calls.append((security.code, dates))
        if security.code == self.fail_code:
            return PublishedHistoryWindow(
                security.code,
                tuple(
                    ProjectHistoryCell(replace(cell, status="unknown_missing", unadjusted=None, qfq=None))
                    for cell in _download(security.code, dates).batch.cells
                ),
            )
        shift = 1.0 if security.code == self.changed_qfq_code else 0.0
        return PublishedHistoryWindow(
            security.code,
            tuple(ProjectHistoryCell(cell) for cell in _download(security.code, dates, shift).batch.cells),
        )

    def fetch_baostock_raw(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> BaoStockCodeDownload:
        self.events.append("baostock_raw")
        value = _download(security.code, dates)
        cells = tuple(replace(cell, qfq=None, status="qfq_missing") for cell in value.batch.cells)
        return BaoStockCodeDownload(BaoStockCodeBatch(security.code, cells), value.daily_facts)

    def fetch_baostock_prices(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> BaoStockCodeDownload:
        self.events.append("baostock_prices")
        return self.fetch_code(security, dates)


def _configuration(root: Path) -> HistorySyncConfiguration:
    return HistorySyncConfiguration(
        root, sessions=3, reread_sessions=2, minimum_free_bytes=0, reference_root=(root).parent / "reference"
    )


@dataclass
class ProgressRecorder:
    values: list[HistorySyncProgress] = field(default_factory=list)

    def publish(self, progress: HistorySyncProgress) -> None:
        self.values.append(progress)


def _snapshot(root):
    result = HistoryControlIndex(root / "catalog.sqlite3", sqlite_policy=SqlitePolicyFixture()).load_state().published_snapshot
    assert result is not None
    return result


@dataclass
class _EligibilitySupplier(FakeSupplier):
    eligible_codes: tuple[str, ...] = ("600001", "600002")
    excluded_status: str = "unknown"

    def load_context(self, as_of, sessions):
        securities = tuple(
            BaoStockSecurity(code, code, "main", date(2000, 1, 1), None, "test") for code in self.eligible_codes
        )
        context = super().load_context(as_of, sessions, universe=securities)
        return replace(
            context,
            source_versions=replace(context.source_versions, dependency_versions=(HISTORY_UNIVERSE_CONTRACT,)),
            st_evidence=tuple(
                HistoryStEvidence(code, as_of, "clear" if code in self.eligible_codes else self.excluded_status)
                for code in ("600001", "600002")
            ),
        )
