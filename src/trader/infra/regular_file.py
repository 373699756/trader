"""Bounded reads from an explicitly owned regular file descriptor."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Protocol, cast


class _WindowsOpenFlags(Protocol):
    O_BINARY: int


def ReadRegularFile(path: Path, *, maximum_bytes: int) -> bytes:
    with _open_regular_file(path, maximum_bytes) as stream:
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
    with _open_regular_file(path, maximum_bytes) as stream:
        yield _bounded_lines(stream, maximum_bytes, maximum_line_bytes, maximum_lines)


@contextmanager
def _open_regular_file(path: Path, maximum_bytes: int) -> Iterator[BinaryIO]:
    if type(maximum_bytes) is not int or maximum_bytes < 1:
        raise ValueError("regular_file_size_limit_invalid")
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
        stream = os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise
    with stream:
        yield stream


def _validate_regular(metadata: os.stat_result, maximum_bytes: int) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("regular_file_required")
    if metadata.st_size > maximum_bytes:
        raise ValueError("regular_file_size_limit")


def _bounded_lines(
    stream: BinaryIO, maximum_bytes: int, maximum_line_bytes: int, maximum_lines: int,
) -> Iterator[bytes]:
    size = count = 0
    while encoded := stream.readline(maximum_line_bytes + 1):
        size += len(encoded)
        count += 1
        if len(encoded) > maximum_line_bytes or size > maximum_bytes or count > maximum_lines:
            raise ValueError("regular_file_read_limit")
        yield encoded


__all__ = ["OpenRegularFileLines", "ReadRegularFile"]
