"""Elapsed-time projection for local qfq history extraction."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager

from trader.download.application.progress_message import ProgressLevel, ProgressMessage
from trader.download.application.qfq_download_progress import QfqMessage


@contextmanager
def QfqLocalProgress(report: Callable[[ProgressMessage], None]) -> Iterator[None]:
    """Keep first-time history verification visible before any code is yielded."""
    report(QfqMessage("历史提取", "等待本地历史窗口", state="开始"))
    succeeded = False
    try:
        yield
        succeeded = True
    finally:
        report(
            QfqMessage(
                "历史提取",
                "完成" if succeeded else "中断",
                level=ProgressLevel.SUCCESS if succeeded else ProgressLevel.WARNING,
                state="完成" if succeeded else "取消", finished=True,
            )
        )
