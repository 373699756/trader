"""Bounded announcement-tool output using the shared process-family owner."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from queue import Empty, Full, Queue
from threading import Event, Thread
from typing import IO

from trader.infra.process_family import ProcessFamily


def CaptureDocumentTool(
    command: tuple[str, ...],
    *,
    maximum_bytes: int,
    check: Callable[[], None],
) -> tuple[int, bytes]:
    """Bound output and cancellation without reaping the leader before group cleanup."""
    check()
    process = ProcessFamily(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        environment={**os.environ, "OMP_THREAD_LIMIT": "1"},
    )
    reader: Thread | None = None
    stopped = Event()
    chunks: Queue[bytes | OSError | None] = Queue(maxsize=2)
    try:
        if process.stdout is None:
            raise RuntimeError("document_tool_stdout_unavailable")
        reader = Thread(target=_read_output, args=(process.stdout, chunks, stopped), name="company-tool-output")
        reader.start()
        output = bytearray()
        while True:
            check()
            try:
                chunk = chunks.get(timeout=0.05)
            except Empty:
                continue
            if chunk is None:
                break
            if isinstance(chunk, OSError):
                raise chunk
            if len(output) + len(chunk) > maximum_bytes:
                raise OverflowError("document_tool_output_limit")
            output.extend(chunk)
        while process.poll() is None:
            check()
            try:
                process.wait(timeout=0.05)
            except subprocess.TimeoutExpired:
                continue
        check()
        result = process.returncode
        if result is None:
            raise RuntimeError("document_tool_exit_unavailable")
        return result, bytes(output)
    finally:
        stopped.set()
        try:
            process.close()
        finally:
            if reader is not None:
                reader.join(timeout=3)
                if reader.is_alive():
                    raise RuntimeError("document_tool_reader_shutdown_failed")
            process.close_streams()


def _read_output(stream: IO[bytes], chunks: Queue[bytes | OSError | None], stopped: Event) -> None:
    while not stopped.is_set():
        try:
            value: bytes | OSError | None = stream.read(65536) or None
        except OSError as exc:
            value = exc
        while not stopped.is_set():
            try:
                chunks.put(value, timeout=0.05)
                break
            except Full:
                continue
        if value is None or isinstance(value, OSError):
            return


__all__ = ["CaptureDocumentTool"]
