"""Elapsed-time projection for local qfq history extraction."""

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from trader.download.application.progress_message import ProgressLevel, ProgressMessage
from trader.download.application.qfq_download_progress import QfqMessage


@contextmanager
def QfqLocalProgress(report: Callable[[ProgressMessage], None], *, interval_seconds: float = 30.0) -> Iterator[None]:
    """Keep first-time history verification visible before any code is yielded."""
    started = time.monotonic()
    stopped = threading.Event()

    def heartbeat() -> None:
        while not stopped.wait(interval_seconds):
            report(QfqMessage(time.monotonic() - started, "历史提取", "等待本地历史窗口"))

    report(QfqMessage(0, "历史提取", "开始"))
    thread = threading.Thread(target=heartbeat, name="qfq-extraction-progress", daemon=False)
    thread.start()
    succeeded = False
    try:
        yield
        succeeded = True
    finally:
        stopped.set()
        thread.join()
        report(
            QfqMessage(
                time.monotonic() - started,
                "历史提取",
                "完成" if succeeded else "中断",
                level=ProgressLevel.SUCCESS if succeeded else ProgressLevel.WARNING,
            )
        )
