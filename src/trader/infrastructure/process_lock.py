"""Cross-platform non-blocking lock for the single local server process."""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from typing import BinaryIO, Protocol, cast


class ProcessLockError(RuntimeError):
    pass


class _WindowsLockApi(Protocol):
    LK_NBLCK: int
    LK_UNLCK: int

    def locking(self, file_descriptor: int, mode: int, byte_count: int) -> None: ...


class ProcessLock:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: BinaryIO | None = None

    def acquire(self) -> None:
        if self._handle is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._path.open("a+b")
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        handle.seek(0)
        try:
            _lock_handle(handle)
        except OSError as exc:
            handle.close()
            raise ProcessLockError(f"trader-server is already running for {self._path.parent}") from exc
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            _unlock_handle(handle)
        finally:
            handle.close()

    def __enter__(self) -> ProcessLock:
        self.acquire()
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


def _lock_handle(handle: BinaryIO) -> None:
    if os.name == "nt":
        windows_lock = cast(_WindowsLockApi, importlib.import_module("msvcrt"))
        windows_lock.locking(handle.fileno(), windows_lock.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_handle(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        windows_lock = cast(_WindowsLockApi, importlib.import_module("msvcrt"))
        windows_lock.locking(handle.fileno(), windows_lock.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


__all__ = ["ProcessLock", "ProcessLockError"]
