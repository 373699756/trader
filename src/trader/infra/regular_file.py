"""Bounded reads from an explicitly owned regular file descriptor."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Protocol, cast


class _WindowsOpenFlags(Protocol):
    O_BINARY: int


class RegularFileStream(Protocol):
    """Only the bounded read operations needed by codecs and digest consumers."""

    def read(self, size: int = -1, /) -> bytes: ...
    def readline(self, size: int = -1, /) -> bytes: ...
    def seek(self, offset: int, whence: int = 0, /) -> int: ...
    def fileno(self) -> int: ...
    def verify(self, path: Path | None = None) -> None: ...


def ReadRegularFile(path: Path, *, maximum_bytes: int) -> bytes:
    with OpenRegularFile(path, maximum_bytes=maximum_bytes) as stream:
        content = stream.read(maximum_bytes + 1)
        if len(content) > maximum_bytes:
            raise ValueError("regular_file_size_limit")
        return content


@contextmanager
def OpenRegularFileLines(
    path: Path, *, maximum_bytes: int, maximum_line_bytes: int, maximum_lines: int,
) -> Iterator[Iterator[bytes]]:
    if (
        type(maximum_line_bytes) is not int or maximum_line_bytes < 1
        or type(maximum_lines) is not int or maximum_lines < 1
    ):
        raise ValueError("regular_file_line_limits_invalid")
    with OpenRegularFile(path, maximum_bytes=maximum_bytes) as stream:
        yield _bounded_lines(stream, maximum_bytes, maximum_line_bytes, maximum_lines)


@contextmanager
def OpenRegularFile(
    path: Path, *, maximum_bytes: int, check: Callable[[], None] | None = None,
) -> Iterator[RegularFileStream]:
    """Own a bounded regular stream; reject replacement, growth and in-place changes."""
    if type(maximum_bytes) is not int or maximum_bytes < 1:
        raise ValueError("regular_file_size_limit_invalid")
    if check is not None:
        check()
    before = path.lstat()
    _validate_regular(before, maximum_bytes)
    flags = os.O_RDONLY
    if os.name == "posix":
        flags |= os.O_NONBLOCK | os.O_NOFOLLOW
    elif os.name == "nt":
        flags |= cast(_WindowsOpenFlags, os).O_BINARY
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        _validate_regular(opened, maximum_bytes)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError("regular_file_identity_changed")
        raw = os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise
    with raw:
        stream = _RegularFileReader(raw, path, maximum_bytes, opened, check)
        yield stream
        stream.verify()


class _RegularFileReader:
    def __init__(
        self,
        raw: BinaryIO,
        path: Path,
        maximum_bytes: int,
        initial: os.stat_result,
        check: Callable[[], None] | None,
    ) -> None:
        self._raw = raw
        self._path = path
        self._maximum_bytes = maximum_bytes
        self._initial = initial
        self._check = check
        self._read_bytes = 0

    def verify(self, path: Path | None = None) -> None:
        if self._check is not None:
            self._check()
        opened = os.fstat(self.fileno())
        current = (path if path is not None else self._path).lstat()
        if not stat.S_ISREG(current.st_mode) or not stat.S_ISREG(opened.st_mode):
            raise ValueError("regular_file_required")
        if opened.st_size > self._maximum_bytes:
            raise ValueError("regular_file_read_limit")
        expected = _identity(self._initial)
        # An owner may rebind only its verified inode after rename changes ctime.
        if path is not None:
            if _identity(opened)[:-1] != expected[:-1] or _identity(current) != _identity(opened):
                raise ValueError("regular_file_identity_changed")
            self._path, self._initial = path, opened
        elif _identity(opened) != expected or _identity(current) != expected:
            raise ValueError("regular_file_identity_changed")

    def _accept(self, content: bytes) -> bytes:
        self._read_bytes += len(content)
        if self._read_bytes > self._maximum_bytes:
            raise ValueError("regular_file_read_limit")
        self.verify()
        return content

    def read(self, size: int = -1) -> bytes:
        self.verify()
        remaining = self._maximum_bytes - self._read_bytes + 1
        requested = remaining if size < 0 else min(size, remaining)
        chunks: list[bytes] = []
        while requested:
            content = self._raw.read(min(requested, 65536))
            content = self._accept(content)
            if not content:
                break
            chunks.append(content)
            requested -= len(content)
        return b"".join(chunks)

    def readline(self, size: int = -1) -> bytes:
        self.verify()
        remaining = self._maximum_bytes - self._read_bytes + 1
        requested = remaining if size < 0 else min(size, remaining)
        return self._accept(self._raw.readline(requested))

    def seek(self, offset: int, whence: int = 0) -> int:
        self.verify()
        return self._raw.seek(offset, whence)

    def fileno(self) -> int:
        return self._raw.fileno()


def _identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns


def _validate_regular(metadata: os.stat_result, maximum_bytes: int) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("regular_file_required")
    if metadata.st_size > maximum_bytes:
        raise ValueError("regular_file_size_limit")


def _bounded_lines(
    stream: RegularFileStream, maximum_bytes: int, maximum_line_bytes: int, maximum_lines: int,
) -> Iterator[bytes]:
    size = count = 0
    while encoded := stream.readline(maximum_line_bytes + 1):
        size += len(encoded)
        count += 1
        if len(encoded) > maximum_line_bytes or size > maximum_bytes or count > maximum_lines:
            raise ValueError("regular_file_read_limit")
        yield encoded


__all__ = [
    "OpenRegularFile",
    "OpenRegularFileLines",
    "RegularFileStream",
    "ReadRegularFile",
]
