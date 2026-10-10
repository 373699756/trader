"""Instance-scoped monotonic coordinates for immutable accepted values."""

from __future__ import annotations

import threading
from typing import Generic, TypeVar

_T = TypeVar("_T")


class ValueRevision(Generic[_T]):
    def __init__(self, prefix: str) -> None:
        self._prefix = prefix
        self._lock = threading.Lock()
        self._value: _T | None = None
        self._revision = 0

    def accept(self, value: _T) -> str:
        with self._lock:
            if self._revision == 0 or self._value != value:
                self._value = value
                self._revision += 1
            return f"{self._prefix}:{self._revision}"
