"""Combine an explicit local cancellation event with its owning operation."""

from __future__ import annotations

import threading
from collections.abc import Callable


class CancellationEvent(threading.Event):
    def __init__(self, cancelled: Callable[[], bool] | None = None) -> None:
        super().__init__()
        self._cancelled = cancelled

    def is_set(self) -> bool:
        return super().is_set() or (self._cancelled is not None and self._cancelled())


__all__ = ["CancellationEvent"]
