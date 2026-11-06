"""Bounded file reads reject special files before any blocking operation."""

import errno
import os
from pathlib import Path

import pytest

from trader.infra import regular_file


def test_regular_reads_preserve_exact_bytes_and_enforce_the_total_limit(tmp_path):
    path = tmp_path / "input"
    path.write_bytes(b"one\ntwo\n")
    assert regular_file.ReadRegularFile(path, maximum_bytes=8) == b"one\ntwo\n"
    with pytest.raises(ValueError, match="size_limit"):
        regular_file.ReadRegularFile(path, maximum_bytes=7)
    with regular_file.OpenRegularFileLines(
        path, maximum_bytes=8, maximum_line_bytes=4, maximum_lines=2,
    ) as lines:
        assert tuple(lines) == (b"one\n", b"two\n")


@pytest.mark.parametrize(("line_bytes", "line_count"), [(3, 2), (4, 1)])
def test_regular_line_reads_enforce_line_and_count_bounds(tmp_path, line_bytes, line_count):
    path = tmp_path / "input"
    path.write_bytes(b"one\ntwo\n")
    with regular_file.OpenRegularFileLines(
        path, maximum_bytes=8, maximum_line_bytes=line_bytes, maximum_lines=line_count,
    ) as lines, pytest.raises(ValueError, match="read_limit"):
        tuple(lines)


@pytest.mark.parametrize("kind", ["directory", "symlink", "fifo"])
def test_special_files_are_rejected_before_os_open(tmp_path, monkeypatch, kind):
    path = tmp_path / "input"
    if kind == "directory":
        path.mkdir()
    elif kind == "symlink":
        target = tmp_path / "target"
        target.write_bytes(b"preserve")
        try:
            path.symlink_to(target)
        except OSError:
            pytest.skip("this platform cannot create a test symlink")
    else:
        if os.name != "posix":
            pytest.skip("POSIX named pipe boundary")
        os.mkfifo(path)
    monkeypatch.setattr(regular_file.os, "open", lambda *_a, **_kw: pytest.fail("special file must not be opened"))
    with pytest.raises(ValueError, match="regular_file_required"):
        regular_file.ReadRegularFile(path, maximum_bytes=1024)


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor race boundary")
def test_file_replaced_by_fifo_does_not_block_and_its_descriptor_is_closed(tmp_path, monkeypatch):
    path = tmp_path / "input"
    path.write_bytes(b"original")
    original_open = os.open
    descriptors = []

    def replace_before_open(value, flags):
        assert Path(value) == path
        assert flags & os.O_NONBLOCK and flags & os.O_NOFOLLOW
        path.unlink()
        os.mkfifo(path)
        descriptor = original_open(value, flags)
        descriptors.append(descriptor)
        return descriptor

    monkeypatch.setattr(regular_file.os, "open", replace_before_open)
    with pytest.raises(ValueError, match="regular_file_required"):
        regular_file.ReadRegularFile(path, maximum_bytes=1024)
    assert len(descriptors) == 1
    with pytest.raises(OSError) as failure:
        os.fstat(descriptors[0])
    assert failure.value.errno == errno.EBADF


def test_file_growth_cannot_evade_the_stream_size_bound(tmp_path):
    path = tmp_path / "input"
    path.write_bytes(b"a\n")
    with regular_file.OpenRegularFileLines(
        path, maximum_bytes=3, maximum_line_bytes=2, maximum_lines=2,
    ) as lines:
        assert next(lines) == b"a\n"
        with path.open("ab") as writer:
            writer.write(b"b\n")
        with pytest.raises(ValueError, match="read_limit"):
            next(lines)
