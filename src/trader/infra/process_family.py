"""Own child process groups without releasing their identity before cleanup."""

from __future__ import annotations

import ctypes
import math
import os
import signal
import subprocess
import sys
import time
from collections.abc import Mapping
from ctypes import wintypes
from pathlib import Path
from typing import IO, Protocol, cast


class ProcessFamilyCapabilityError(RuntimeError):
    """The runtime cannot retain an owned child's identity until group cleanup."""


class _WindowsFunction(Protocol):
    argtypes: list[object]
    restype: object

    def __call__(self, *args: object) -> int: ...


class _WindowsKernel(Protocol):
    CreateJobObjectW: _WindowsFunction
    SetInformationJobObject: _WindowsFunction
    AssignProcessToJobObject: _WindowsFunction
    CloseHandle: _WindowsFunction
    TerminateJobObject: _WindowsFunction
    QueryInformationJobObject: _WindowsFunction


class _WindowsCtypes(Protocol):
    def WinDLL(self, name: str, *, use_last_error: bool) -> _WindowsKernel: ...
    def WinError(self, code: int) -> OSError: ...
    def get_last_error(self) -> int: ...


class _WindowsChildHandle(Protocol):
    # CPython's Windows Popen owns this native handle until its own destruction.
    # Assign the created process handle, never reopen a potentially reused PID.
    _handle: int


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


class _WindowsJob:
    def __init__(self, process_handle: int) -> None:
        windows = cast(_WindowsCtypes, ctypes)
        self._windows = windows
        kernel = windows.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.TerminateJobObject.restype = wintypes.BOOL
        kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
        kernel.QueryInformationJobObject.restype = wintypes.BOOL
        self._kernel = kernel
        self._job: int | None = None
        job = kernel.CreateJobObjectW(None, None)
        if not job:
            raise windows.WinError(windows.get_last_error())
        try:
            limits = _ExtendedLimits()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise windows.WinError(windows.get_last_error())
            if not kernel.AssignProcessToJobObject(job, process_handle):
                raise windows.WinError(windows.get_last_error())
        except BaseException:
            kernel.CloseHandle(job)
            raise
        self._job = job

    def close(self) -> None:
        if self._job is None:
            return
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
                    return
                if time.monotonic() >= deadline:
                    raise RuntimeError("process_family_shutdown_failed")
                time.sleep(0.01)
        finally:
            if not self._kernel.CloseHandle(job):
                raise self._windows.WinError(self._windows.get_last_error())


class ProcessFamily:
    """Create one owned family; observations never release its POSIX leader PID.

    Callers receive output streams and non-reaping poll/wait operations, not the
    underlying Popen. Cleanup sends the final group signal before the sole reap.
    On Windows the gated worker joins the job before starting the requested tool.
    """

    def __init__(
        self,
        argv: tuple[str, ...],
        *,
        stdout: int | IO[bytes] | None = None,
        stderr: int | IO[bytes] | None = None,
        cwd: Path | None = None,
        environment: Mapping[str, str] | None = None,
        graceful_signal: int = signal.SIGTERM,
        grace_seconds: float = 1.0,
        reap_seconds: float = 2.0,
    ) -> None:
        if not argv or not all(math.isfinite(value) for value in (grace_seconds, reap_seconds)):
            raise ValueError("process_family_options_invalid")
        if grace_seconds < 0 or reap_seconds <= 0:
            raise ValueError("process_family_options_invalid")
        if os.name == "posix":
            if not all(hasattr(os, name) for name in ("waitid", "WNOWAIT", "WEXITED", "WNOHANG", "P_PID")):
                raise ProcessFamilyCapabilityError("process_family_nonreaping_wait_unavailable")
            if signal.getsignal(signal.SIGCHLD) == signal.SIG_IGN:
                raise ProcessFamilyCapabilityError("process_family_sigchld_ignored")
        elif os.name != "nt":
            raise ProcessFamilyCapabilityError("process_family_platform_unsupported")
        self._argv = argv
        self._graceful_signal = graceful_signal
        self._grace_seconds = grace_seconds
        self._reap_seconds = reap_seconds
        self._returncode: int | None = None
        self._closed = False
        self._job: _WindowsJob | None = None
        gated = os.name == "nt"
        invocation = (sys.executable, "-m", "trader.infra.process_family", *argv) if gated else argv
        self._process: subprocess.Popen[bytes] = subprocess.Popen(
            invocation,
            stdin=subprocess.PIPE if gated else subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            cwd=cwd,
            env=environment,
            start_new_session=os.name == "posix",
        )
        if gated:
            try:
                self._job = _WindowsJob(cast(_WindowsChildHandle, self._process)._handle)
                if self._process.stdin is None:
                    raise RuntimeError("process_family_gate_unavailable")
                self._process.stdin.write(b"1")
                self._process.stdin.close()
            except BaseException:
                try:
                    if self._job is not None:
                        self._job.close()
                    else:
                        self._process.kill()
                    self._process.wait(timeout=reap_seconds)
                finally:
                    self.close_streams()
                raise

    @property
    def pid(self) -> int:
        return self._process.pid

    @property
    def stdout(self) -> IO[bytes] | None:
        return self._process.stdout

    @property
    def returncode(self) -> int | None:
        return self._returncode

    def poll(self) -> int | None:
        """Observe completion while keeping the POSIX leader unreaped and un-reusable."""
        if self._closed:
            return self._returncode
        if os.name == "nt":
            self._returncode = self._process.poll()
        else:
            try:
                status = os.waitid(os.P_PID, self.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            except ChildProcessError as exc:
                # An external reaper invalidated our ownership. Never signal that number.
                raise RuntimeError("process_family_leader_identity_lost") from exc
            if status is not None:
                self._returncode = status.si_status if status.si_code == os.CLD_EXITED else -status.si_status
        return self._returncode

    def wait(self, timeout: float) -> int:
        """Wait for completion without reaping; close owns the single final reap."""
        deadline = time.monotonic() + timeout
        while (result := self.poll()) is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(self._argv, timeout)
            time.sleep(min(0.01, remaining))
        return result

    def close(self) -> None:
        if self._closed:
            return
        if self._job is not None:
            self._job.close()
            self._job = None
        elif os.name == "posix":
            # poll retained a zombie, or the leader is still live. Both pin PGID.
            running = self.poll() is None
            self._signal_group(self._graceful_signal)
            if running:
                try:
                    self.wait(self._grace_seconds)
                except subprocess.TimeoutExpired:
                    pass
            self._signal_group(signal.SIGKILL)
        else:
            if self._process.poll() is None:
                self._process.kill()
        self._returncode = self._process.wait(timeout=self._reap_seconds)
        self._closed = True

    def _signal_group(self, signum: int) -> None:
        try:
            os.killpg(self.pid, signum)
        except ProcessLookupError:
            pass

    def close_streams(self) -> None:
        for stream in (self._process.stdin, self._process.stdout, self._process.stderr):
            if stream is not None:
                stream.close()


def _worker() -> int:
    if sys.stdin.buffer.read(1) != b"1" or len(sys.argv) < 2:
        return 2
    return subprocess.call(sys.argv[1:], stdin=subprocess.DEVNULL)


if __name__ == "__main__":
    raise SystemExit(_worker())


__all__ = [
    "ProcessFamily",
    "ProcessFamilyCapabilityError",
]
