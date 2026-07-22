"""Shared atomic JSON persistence helpers."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from trader.application.workers import BoundedExecutor


class RuntimeJsonWriter:
    """Serialize runtime JSON writes on the pipeline persistence executor."""

    def __init__(self, executor: BoundedExecutor) -> None:
        if executor.worker_count != 1:
            raise ValueError("runtime JSON writer requires a single-worker executor")
        self._executor = executor

    def write(self, path: Path, payload: object) -> None:
        if not self._executor.is_running() or self._executor.owns_current_thread():
            atomic_write_json(path, payload)
            return
        future = self._executor.submit(atomic_write_json, path, payload)
        if future is None:
            raise RuntimeError("persistence queue rejected runtime JSON write")
        future.result()


def atomic_write_json(path: Path, payload: object) -> None:
    """Write a JSON payload atomically.

    A sibling of the existing private atomic writer used by snapshot persistence,
    this helper is intentionally small and reusable for cache-like persistence.
    """

    text = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def atomic_read_json(path: Path) -> object:
    """Read and decode a JSON file into Python objects."""

    return json.loads(path.read_text(encoding="utf-8"))


__all__ = ["RuntimeJsonWriter", "atomic_read_json", "atomic_write_json"]
