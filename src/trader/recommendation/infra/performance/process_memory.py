"""Read current resident memory and process peak without conflating the two."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PerformanceMemorySample:
    current_rss_bytes: int
    peak_rss_bytes: int

    def __post_init__(self) -> None:
        if min(self.current_rss_bytes, self.peak_rss_bytes) <= 0:
            raise ValueError("performance process memory readings must be positive")


def ReadPerformanceMemory() -> PerformanceMemorySample:
    """Take a bounded platform reading; sample the peak after current RSS."""

    if os.name == "nt":
        return _windows_memory()
    if sys.platform.startswith("linux"):
        fields = Path("/proc/self/statm").read_text(encoding="ascii").split()
        if len(fields) < 2:
            raise RuntimeError("performance current RSS is unavailable")
        current = int(fields[1]) * int(os.sysconf("SC_PAGE_SIZE"))
        return PerformanceMemorySample(current, _resource_peak())
    if sys.platform == "darwin":
        completed = subprocess.run(
            ("ps", "-o", "rss=", "-p", str(os.getpid())),
            capture_output=True,
            text=True,
            check=True,
            timeout=1.0,
        )
        current = int(completed.stdout.strip()) * 1024
        return PerformanceMemorySample(current, _resource_peak())
    raise RuntimeError("performance process memory platform is unsupported")


def _resource_peak() -> int:
    import resource

    peak = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return peak if sys.platform == "darwin" else peak * 1024


def _windows_memory() -> PerformanceMemorySample:
    import ctypes
    from ctypes import wintypes

    size_t = ctypes.c_size_t

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = (
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", size_t),
            ("WorkingSetSize", size_t),
            ("QuotaPeakPagedPoolUsage", size_t),
            ("QuotaPagedPoolUsage", size_t),
            ("QuotaPeakNonPagedPoolUsage", size_t),
            ("QuotaNonPagedPoolUsage", size_t),
            ("PagefileUsage", size_t),
            ("PeakPagefileUsage", size_t),
        )

    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    win_dll = ctypes.__dict__["WinDLL"]
    kernel32 = win_dll("kernel32", use_last_error=True)
    psapi = win_dll("psapi", use_last_error=True)
    get_current_process = kernel32.GetCurrentProcess
    get_current_process.restype = wintypes.HANDLE
    get_process_memory_info = psapi.GetProcessMemoryInfo
    get_process_memory_info.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(ProcessMemoryCounters),
        wintypes.DWORD,
    )
    get_process_memory_info.restype = wintypes.BOOL
    if not get_process_memory_info(get_current_process(), ctypes.byref(counters), counters.cb):
        raise ctypes.__dict__["WinError"](ctypes.__dict__["get_last_error"]())
    return PerformanceMemorySample(int(counters.WorkingSetSize), int(counters.PeakWorkingSetSize))


__all__ = ["PerformanceMemorySample", "ReadPerformanceMemory"]
