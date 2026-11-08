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
    with pytest.raises(ValueError, match="read_limit"):
        with regular_file.OpenRegularFileLines(
            path, maximum_bytes=3, maximum_line_bytes=2, maximum_lines=2,
        ) as lines:
            assert next(lines) == b"a\n"
            with path.open("ab") as writer:
                writer.write(b"b\n")
            next(lines)


@pytest.mark.parametrize("change", ["replace", "rewrite", "grow"])
def test_stream_rejects_identity_or_content_changes_after_open(tmp_path, change):
    path = tmp_path / "input"
    path.write_bytes(b"initial")
    with pytest.raises(ValueError, match="regular_file_identity_changed"):
        with regular_file.OpenRegularFile(path, maximum_bytes=32) as stream:
            assert stream.read(1) == b"i"
            if change == "replace":
                replacement = tmp_path / "replacement"
                replacement.write_bytes(b"changed")
                os.replace(replacement, path)
            elif change == "rewrite":
                metadata = path.stat()
                path.write_bytes(b"changed")
                os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1000000000))
            else:
                with path.open("ab") as writer:
                    writer.write(b"new")
            stream.read(1)


def test_stream_checks_cancellation_between_chunks_and_closes_descriptor(tmp_path):
    path = tmp_path / "input"
    path.write_bytes(b"a" * 200000)
    calls = 0

    def check():
        nonlocal calls
        calls += 1
        if calls == 3:
            raise InterruptedError("fixture_cancelled")

    with pytest.raises(InterruptedError, match="fixture_cancelled"):
        with regular_file.OpenRegularFile(path, maximum_bytes=200000, check=check) as stream:
            descriptor = stream.fileno()
            stream.read()
    assert calls == 3
    with pytest.raises(OSError) as failure:
        os.fstat(descriptor)
    assert failure.value.errno == errno.EBADF


def test_stream_keeps_a_total_read_budget_across_seeks(tmp_path):
    path = tmp_path / "input"
    path.write_bytes(b"1234")
    with pytest.raises(ValueError, match="read_limit"):
        with regular_file.OpenRegularFile(path, maximum_bytes=4) as stream:
            assert stream.read() == b"1234"
            stream.seek(0)
            stream.read(1)


def test_stream_creation_failure_closes_the_owned_descriptor(tmp_path, monkeypatch):
    path = tmp_path / "input"
    path.write_bytes(b"preserved")
    descriptors = []

    def failed_open(descriptor, _mode):
        descriptors.append(descriptor)
        raise RuntimeError("fixture_fdopen_failed")

    monkeypatch.setattr(regular_file.os, "fdopen", failed_open)
    with pytest.raises(RuntimeError, match="fixture_fdopen_failed"):
        with regular_file.OpenRegularFile(path, maximum_bytes=1024):
            pytest.fail("failed stream must not be yielded")
    assert len(descriptors) == 1
    with pytest.raises(OSError) as failure:
        os.fstat(descriptors[0])
    assert failure.value.errno == errno.EBADF
