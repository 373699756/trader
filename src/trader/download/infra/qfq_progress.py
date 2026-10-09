"""Bounded supplier heartbeat and elapsed-time projection for qfq maintenance."""

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from trader.download.domain.history_sync import HistorySyncProgress


class QfqSupplierProgress:
    def __init__(self, report: Callable[[str], None]) -> None:
        self._report = report

    def publish(self, progress: HistorySyncProgress) -> None:
        self._report(
            f"qfq {progress.stage}: {progress.state} attempt={progress.attempt}/{progress.max_attempts} "
            f"elapsed={progress.call_elapsed_seconds:.1f}s"
        )


@contextmanager
def qfq_local_progress(report: Callable[[str], None], *, interval_seconds: float = 30.0) -> Iterator[None]:
    """Keep first-time history verification visible before any code is yielded."""
    started = time.monotonic()
    stopped = threading.Event()

    def heartbeat() -> None:
        while not stopped.wait(interval_seconds):
            report(f"qfq history extraction: waiting elapsed={time.monotonic() - started:.1f}s")

    report("qfq history extraction: started")
    thread = threading.Thread(target=heartbeat, name="qfq-extraction-progress", daemon=False)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join()
        report(f"qfq history extraction: finished elapsed={time.monotonic() - started:.1f}s")
