from __future__ import annotations
from tests.helpers.shared.configuration import SqlitePolicyFixture
import hashlib
import re
import shutil
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from tests.helpers.download.qfq import QFQ_CONFIGURATION
from tests.helpers.shared.reference_fixtures import PreparedFixture
from trader.download.application.progress_events import DownloadProgressEvent
from trader.download.infra.progress_projection import ProjectDownloadProgress
from trader.download.application.update_qfq import UpdateQfqWindows
from trader.download.domain.baostock_daily import (
    BaoStockCalendar,
    BaoStockDailyCell,
    BaoStockDailySide,
    BaoStockSecurity,
    BaoStockSourceVersions,
)
from trader.download.domain.history_sync import HistorySupplierContext
from trader.download.domain.price_review import PriceReviewRequestBudget
from trader.download.domain.published_history import (
    ProjectHistoryCell,
    PublishedHistoryWindow,
)
from trader.download.domain.qfq_window import QfqWindowIncompleteError
from trader.download.infra.download_settings import LoadHistorySyncConfiguration, LoadQfqWindowConfiguration
from trader.download.infra.qfq_sqlite import SQLiteQfqWindowCache
from trader.download.infra.terminal_progress import RenderProgress
from trader.infra.settings.data_paths import DataPaths


def _plain_feedback(event: DownloadProgressEvent) -> str:
    # Existing behavior assertions ignore padding; separate terminal tests check exact cell positions.
    message = ProjectDownloadProgress(event)
    identity = "" if message.task is None else f"{message.task.owner}·{message.task.label} | {message.state} | "
    line = re.sub(r" +", " ", identity + RenderProgress(message))
    return re.sub(r" */ *", "/", re.sub(r"([（(]) +", r"\1", line))


SHANGHAI = ZoneInfo("Asia/Shanghai")

DAYS = tuple(date(2025, 1, 1) + timedelta(days=offset) for offset in range(252))

PROJECT_ROOT = Path(__file__).resolve().parents[4]


def _install_download_config(project_root: Path) -> None:
    config_root = project_root / "config"
    config_root.mkdir(parents=True, exist_ok=True)
    shutil.copy2(PROJECT_ROOT / "config/download.json", config_root / "download.json")


def _qfq_download_configuration(project_root: Path, paths: DataPaths):
    return LoadHistorySyncConfiguration(project_root, paths), LoadQfqWindowConfiguration(project_root)


def _cell(code: str, day: date, *, price: float = 10.0) -> BaoStockDailyCell:
    raw = BaoStockDailySide(
        code,
        day,
        "unadjusted",
        price,
        price + 0.2,
        price - 0.2,
        price,
        100000.0,
        10000000.0,
        price - 0.01,
        0.1,
        1.0,
        "trading",
    )
    qfq = replace(raw, adjustment="qfq", preclose=None, pct_change=None, turnover=None)
    return BaoStockDailyCell(code, day, "complete", raw, qfq)


def _window(code: str = "600001", days: tuple[date, ...] = DAYS, *, price: float = 10.0) -> PublishedHistoryWindow:
    return PublishedHistoryWindow(code, tuple(ProjectHistoryCell(_cell(code, day, price=price)) for day in days))


def _fingerprints(root: Path) -> dict[str, tuple[str, int]]:
    return {
        path.name: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
        for path in root.iterdir()
        if path.is_file() and path.suffix == ".sqlite3"
    }


def _reference(codes=("600001",)):
    return PreparedFixture(
        datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI),
        tuple(BaoStockSecurity(code, "fixture", "main", DAYS[0], None, "fixture") for code in codes),
        open_dates=DAYS,
    )


@dataclass
class Supplier:
    days: tuple[date, ...] = DAYS[1:]
    codes: tuple[str, ...] = ("600001",)
    conflict: bool = False
    fail: bool = False
    requested_sessions: int = 251
    calls: list[tuple[str, tuple[date, ...]]] = field(default_factory=list)

    def load_qfq_context(self, as_of: date, sessions: int) -> HistorySupplierContext:
        assert sessions == self.requested_sessions
        return HistorySupplierContext(
            BaoStockCalendar(self.days),
            tuple(BaoStockSecurity(code, "fixture", "main", DAYS[0], None, "fixture") for code in self.codes),
            BaoStockSourceVersions("fixture", "3.11", ()),
        )

    def fetch_window(
        self, security: BaoStockSecurity, dates: tuple[date, ...], *, request_budget: PriceReviewRequestBudget | None = None
    ) -> PublishedHistoryWindow:
        self.calls.append((security.code, dates))
        if self.fail:
            raise QfqWindowIncompleteError(dates, dates)
        cells = tuple(_cell(security.code, day, price=11.0 if self.conflict else 10.0) for day in dates)
        return PublishedHistoryWindow(security.code, tuple(ProjectHistoryCell(cell) for cell in cells))


def _updater(tmp_path: Path, supplier: Supplier) -> UpdateQfqWindows:
    return UpdateQfqWindows(
        SQLiteQfqWindowCache((tmp_path) / "daily_window.sqlite3", "v2", sqlite_policy=SqlitePolicyFixture()),
        SQLiteQfqWindowCache((tmp_path) / "daily_window.sqlite3", "v3", sqlite_policy=SqlitePolicyFixture()),
        supplier,
        lambda: False,
        lambda _message: None,
        QFQ_CONFIGURATION,
    )


class _IncompleteSupplier(Supplier):
    def fetch_window(
        self, security: BaoStockSecurity, dates: tuple[date, ...], *, request_budget: PriceReviewRequestBudget | None = None
    ) -> PublishedHistoryWindow:
        window = super().fetch_window(security, dates, request_budget=request_budget)
        if security.code == "600001":
            raise QfqWindowIncompleteError(dates[:4], dates[:4])
        return window
