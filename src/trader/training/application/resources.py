"""Explicit reservation boundary for concurrent fitting allocations."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Protocol


class TrainingMemoryAdmission(Protocol):
    def reserve(self, required_bytes: int, heartbeat: Callable[[], None]) -> AbstractContextManager[None]: ...
