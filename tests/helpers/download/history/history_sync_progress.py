from __future__ import annotations
import re
from dataclasses import replace
from io import StringIO
from pathlib import Path
from trader.download.application.stock_download_progress import StockDownloadProgress
from trader.download.domain.history_maintenance import HistoryMaintenanceStatus


def _compact_log(value: str) -> str:
    value = re.sub(r" +", " ", value)
    return re.sub(r" */ *", "/", re.sub(r"([（(]) +", r"\1", value))


class _Clock:
    def __init__(self, *values: float) -> None:
        self._values = iter(values)

    def __call__(self) -> float:
        return next(self._values)


def _failed_status(reason: str) -> HistoryMaintenanceStatus:
    return HistoryMaintenanceStatus("failed", reason, Path("data/history"), "baostock", None, None, None, None)


def _stock_progress(**changes) -> StockDownloadProgress:
    return replace(
        StockDownloadProgress(
            "sync history",
            9,
            4400,
            "688058",
            "宝兰德",
            "Tencent下载",
            "合格",
            2000,
            2000,
            2000,
            2000,
            0,
        ),
        **changes,
    )


class _Terminal(StringIO):
    def __init__(self, tty: bool):
        super().__init__()
        self.tty = tty
        self.writes = 0

    def isatty(self):
        return self.tty

    def write(self, text):
        self.writes += 1
        return super().write(text)
