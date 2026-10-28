"""Atomic JSON file primitives used by runtime adapters."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Protocol, cast

from trader.infra.workers import BoundedExecutor


class _WindowsMove(Protocol):
    argtypes: tuple[object, ...]
    restype: object

    def __call__(self, source: str, destination: str, flags: int) -> int: ...


class _WindowsKernel(Protocol):
    MoveFileExW: _WindowsMove


class _WindowsCtypes(Protocol):
    def WinDLL(self, name: str, *, use_last_error: bool) -> _WindowsKernel: ...
    def WinError(self, code: int) -> OSError: ...
    def get_last_error(self) -> int: ...


class RuntimeJsonWriter:
    """Serialize runtime JSON writes on the pipeline persistence executor."""

    def __init__(self, executor: BoundedExecutor) -> None:
        if executor.worker_count != 1:
            raise ValueError("runtime JSON writer requires a single-worker executor")
        self._executor = executor

    def write(self, path: Path, payload: object) -> None:
        if not self._executor.is_running() or self._executor.owns_current_thread():
            AtomicWriteJson(path, payload)
            return
        future = self._executor.submit(AtomicWriteJson, path, payload)
        if future is None:
            raise RuntimeError("persistence queue rejected runtime JSON write")
        future.result()


def AtomicWriteJson(path: Path, payload: object) -> None:
    """Write a JSON payload atomically beside the destination file."""

    text = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    missing = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    path.parent.mkdir(parents=True, exist_ok=True)
    for created in reversed(missing):
        SyncDirectory(created.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "nt":
            _replace_windows(temporary_name, path)
        else:
            os.replace(temporary_name, path)
        SyncDirectory(path.parent)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def AtomicReadJson(path: Path) -> object:
    """Read and decode a JSON file using UTF-8."""

    return json.loads(path.read_text(encoding="utf-8"))


def SyncDirectory(path: Path) -> None:
    """Persist directory entries on POSIX; Windows has no portable directory fsync."""
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _replace_windows(source: str, destination: Path) -> None:
    # MoveFileExW explicitly requests write-through; file contents were fsynced
    # above. A successful rename alone is not a durability acknowledgement.
    import ctypes
    from ctypes import wintypes

    windows = cast(_WindowsCtypes, ctypes)
    kernel = windows.WinDLL("kernel32", use_last_error=True)
    move = kernel.MoveFileExW
    move.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
    move.restype = wintypes.BOOL
    if not move(source, str(destination), 0x1 | 0x8):
        raise windows.WinError(windows.get_last_error())


__all__ = ["RuntimeJsonWriter", "AtomicReadJson", "AtomicWriteJson", "SyncDirectory"]
