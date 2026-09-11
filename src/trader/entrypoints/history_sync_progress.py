"""Stderr projection for interactive history synchronization progress."""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable

from trader.application.research.history_sync import HistorySyncProgress


class StderrHistorySyncProgress:
    def __init__(self, *, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._monotonic = monotonic
        self._started_at = monotonic()

    def publish(self, progress: HistorySyncProgress) -> None:
        percent = 100.0 if progress.total_units == 0 else progress.completed_units / progress.total_units * 100.0
        print(
            json.dumps(
                {
                    "schema_version": "history_sync_progress",
                    "stage": progress.stage,
                    "state": progress.state,
                    "completed_units": progress.completed_units,
                    "total_units": progress.total_units,
                    "percent": round(percent, 2),
                    "current_item": progress.current_item,
                    "attempt": progress.attempt,
                    "max_attempts": progress.max_attempts,
                    "call_elapsed_seconds": round(progress.call_elapsed_seconds, 3),
                    "elapsed_seconds": round(self._monotonic() - self._started_at, 3),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
            flush=True,
        )


__all__ = ["StderrHistorySyncProgress"]
