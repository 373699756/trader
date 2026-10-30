"""Bounded announcement-tool output and explicit process-family ownership."""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from ctypes import wintypes
from queue import Empty, Full, Queue
from threading import Event, Thread
from typing import BinaryIO, Protocol, cast


class _WindowsFunction(Protocol):
    argtypes: list[object]
    restype: object

    def __call__(self, *args: object) -> int: ...


class _WindowsKernel(Protocol):
    CreateJobObjectW: _WindowsFunction
    SetInformationJobObject: _WindowsFunction
    OpenProcess: _WindowsFunction
    AssignProcessToJobObject: _WindowsFunction
    CloseHandle: _WindowsFunction
    TerminateJobObject: _WindowsFunction
    QueryInformationJobObject: _WindowsFunction


class _WindowsCtypes(Protocol):
    def WinDLL(self, name: str, *, use_last_error: bool) -> _WindowsKernel: ...
    def WinError(self, code: int) -> OSError: ...
    def get_last_error(self) -> int: ...


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
        ("flags", wintypes.DWORD), ("minimum_working_set", ctypes.c_size_t),
        ("maximum_working_set", ctypes.c_size_t), ("process_limit", wintypes.DWORD),
        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in ("read_ops", "write_ops", "other_ops", "read", "write", "other")]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", _BasicLimits), ("io", _IoCounters),
        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t),
    ]


class _JobAccounting(ctypes.Structure):
    _fields_ = [
        ("user_time", ctypes.c_int64), ("kernel_time", ctypes.c_int64),
        ("period_user_time", ctypes.c_int64), ("period_kernel_time", ctypes.c_int64),
        ("page_faults", wintypes.DWORD), ("total", wintypes.DWORD),
        ("running", wintypes.DWORD), ("terminated", wintypes.DWORD),
    ]


class DocumentProcessFamily:
    """Attach before feeding the parser stdin, so its tools inherit the Windows job."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self._process = process
        self._job: int | None = None
        if os.name != "nt":
            return
        windows = cast(_WindowsCtypes, ctypes)
        self._windows = windows
        kernel = windows.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.TerminateJobObject.restype = wintypes.BOOL
        kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
        kernel.QueryInformationJobObject.restype = wintypes.BOOL
        self._kernel = kernel
        job = kernel.CreateJobObjectW(None, None)
        if not job:
            raise windows.WinError(windows.get_last_error())
        try:
            limits = _ExtendedLimits()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise windows.WinError(windows.get_last_error())
            handle = kernel.OpenProcess(0x0101, False, process.pid)  # SET_QUOTA | TERMINATE
            if not handle:
                raise windows.WinError(windows.get_last_error())
            try:
                if not kernel.AssignProcessToJobObject(job, handle):
                    raise windows.WinError(windows.get_last_error())
            finally:
                kernel.CloseHandle(handle)
        except BaseException:
            kernel.CloseHandle(job)
            raise
        self._job = job

    def close(self) -> None:
        process = self._process
        if self._job is not None:
            job, self._job = self._job, None
            try:
                if not self._kernel.TerminateJobObject(job, 1):
                    raise self._windows.WinError(self._windows.get_last_error())
                deadline = time.monotonic() + 2
                while True:
                    accounting = _JobAccounting()
                    if not self._kernel.QueryInformationJobObject(job, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None):
                        raise self._windows.WinError(self._windows.get_last_error())
                    if accounting.running == 0:
                        break
                    if time.monotonic() >= deadline:
                        raise RuntimeError("document_process_family_shutdown_failed")
                    time.sleep(0.01)
            finally:
                if not self._kernel.CloseHandle(job):
                    raise self._windows.WinError(self._windows.get_last_error())
        elif os.name == "posix":
            # Signal the group even when the leader has already exited.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif process.poll() is None:
            process.kill()
        process.wait(timeout=2)


def CaptureDocumentTool(
    command: tuple[str, ...],
    *,
    maximum_bytes: int,
    check: Callable[[], None],
) -> tuple[int, bytes]:
    """Drain a bounded queue; cancellation and caps terminate before joining the reader."""
    check()
    # Windows children must not execute before assignment to the kill-on-close job.
    # The tiny worker blocks on stdin; all actual tools inherit its job membership.
    gated = os.name == "nt"
    invocation = (sys.executable, "-m", "trader.download.infra.company_document_process", *command) if gated else command
    process: subprocess.Popen[bytes] = subprocess.Popen(
        invocation, stdin=subprocess.PIPE if gated else subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, start_new_session=os.name == "posix",
        env={**os.environ, "OMP_THREAD_LIMIT": "1"},
    )
    family: DocumentProcessFamily | None = None
    reader: Thread | None = None
    stopped = Event()
    chunks: Queue[bytes | OSError | None] = Queue(maxsize=2)
    try:
        family = DocumentProcessFamily(process)
        if gated and process.stdin is not None:
            process.stdin.write(b"1")
            process.stdin.close()
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
        return process.returncode, bytes(output)
    finally:
        stopped.set()
        try:
            if family is not None:
                family.close()
            elif process.poll() is None:
                process.kill()
                process.wait(timeout=2)
        finally:
            if reader is not None:
                reader.join(timeout=3)
                if reader.is_alive():
                    raise RuntimeError("document_tool_reader_shutdown_failed")
            if process.stdout is not None:
                process.stdout.close()
            if process.stdin is not None:
                process.stdin.close()


def _read_output(stream: BinaryIO, chunks: Queue[bytes | OSError | None], stopped: Event) -> None:
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


def _worker() -> int:
    if sys.stdin.buffer.read(1) != b"1" or len(sys.argv) < 2:
        return 2
    return subprocess.call(sys.argv[1:], stdin=subprocess.DEVNULL)


if __name__ == "__main__":
    raise SystemExit(_worker())


__all__ = ["CaptureDocumentTool", "DocumentProcessFamily"]
