"""Cross-platform non-blocking lock for the single local server process."""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from typing import BinaryIO, Protocol, cast

from trader.infra.atomic_files.json import SyncDirectory


class ProcessLockError(RuntimeError):
    pass


class _WindowsLockApi(Protocol):
    LK_NBLCK: int
    LK_UNLCK: int

    def locking(self, file_descriptor: int, mode: int, byte_count: int) -> None: ...


class ProcessLock:
    def __init__(self, path: Path, *, label: str = "trader-server") -> None:
        self._path = path
        self._label = label
        self._handle: BinaryIO | None = None

    def acquire(self) -> None:
        if self._handle is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        handle = os.fdopen(descriptor, "r+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
            try:
                _lock_handle(handle)
            except OSError as exc:
                message = f"{self._label} is already running"
                if self._label == "trader-server":
                    message += f" for {self._path.parent}"
                raise ProcessLockError(message) from exc
        except BaseException:
            handle.close()
            raise
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

    def read_identity(self) -> str:
        """Read a bounded owner identity while retaining the locked inode."""
        if self._handle is None:
            raise RuntimeError("process lock is not held")
        self._handle.seek(1)
        value = self._handle.read(4097)
        if len(value) > 4096:
            raise ValueError("process lock identity too large")
        return value.decode("utf-8")

    def write_identity(self, identity: str) -> None:
        """Bind an owner location, without replacing the lock or storing counters."""
        if self._handle is None:
            raise RuntimeError("process lock is not held")
        value = identity.encode("utf-8")
        if len(value) > 4096:
            raise ValueError("process lock identity too large")
        self._handle.seek(1)
        self._handle.write(value)
        self._handle.truncate()
        self._handle.flush()
        os.fsync(self._handle.fileno())
        SyncDirectory(self._path.parent)

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
