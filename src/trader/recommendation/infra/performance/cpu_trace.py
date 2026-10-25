"""Opt-in CPU/GC evidence with flushed progress for interrupted offline replays."""

from __future__ import annotations

import cProfile
import gc
import json
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from types import TracebackType
from typing import TextIO, TypeVar

_T = TypeVar("_T")


class PerformanceCpuTrace:
    def __init__(self, directory: Path, *, functions: bool = True) -> None:
        self._directory = directory
        self._functions = functions
        self._lock = threading.RLock()
        self._events: TextIO | None = None
        self._gc_started = 0.0

    def __enter__(self) -> PerformanceCpuTrace:
        self._directory.mkdir(parents=True, exist_ok=True)
        self._events = (self._directory / "progress.jsonl").open("w", encoding="utf-8")
        try:
            gc.callbacks.append(self._on_gc)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        if self._on_gc in gc.callbacks:
            gc.callbacks.remove(self._on_gc)
        if self._events is not None:
            self._events.close()

    def _on_gc(self, phase: str, info: dict[str, int]) -> None:
        if phase == "start":
            self._gc_started = time.perf_counter()
        else:
            self._write("gc", f"generation_{info['generation']}", (time.perf_counter() - self._gc_started) * 1000)

    def _write(self, event: str, phase: str, elapsed_ms: float = 0.0) -> None:
        payload = json.dumps(
            {
                "event": event,
                "phase": phase,
                "at": time.perf_counter(),
                "elapsed_ms": elapsed_ms,
                "thread": threading.get_ident(),
            }
        )
        with self._lock:
            if self._events is not None:
                self._events.write(payload + "\n")
                self._events.flush()

    def record(self, name: str, result: Mapping[str, object]) -> None:
        """Persist completed round projections even if a later round is interrupted."""
        payload = json.dumps({"event": "round", "phase": name, "result": result})
        with self._lock:
            if self._events is not None:
                self._events.write(payload + "\n")
                self._events.flush()

    def run(self, name: str, operation: Callable[[], _T]) -> _T:
        self._write("start", name)
        profiler = cProfile.Profile() if self._functions else None
        started = time.perf_counter()
        try:
            return operation() if profiler is None else profiler.runcall(operation)
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            if profiler is not None:
                profiler.dump_stats(str(self._directory / f"{name}.pstats"))
            self._write("end", name, elapsed_ms)


__all__ = ["PerformanceCpuTrace"]
