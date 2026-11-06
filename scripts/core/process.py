"""Bounded child processes shared by tools; no business imports."""
from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
from collections.abc import Mapping

from scripts.core.paths import PROJECT_ROOT
from trader.infra.process_family import ProcessFamily


class DiagnosticOutputLimitError(ValueError):
    """Captured child output exceeded the shared tool limit."""


def RunBoundedCommand(
    argv: tuple[str, ...], *, timeout: float, environment: Mapping[str, str]
) -> subprocess.CompletedProcess[str]:
    """Bound execution/output; allow five extra seconds for owned worker cleanup on POSIX."""
    limit = 20 * 1024 * 1024
    started = time.monotonic()
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        process = ProcessFamily(
            argv, cwd=PROJECT_ROOT, stdout=output, stderr=errors, environment=environment,
            graceful_signal=signal.SIGINT, grace_seconds=5.0, reap_seconds=5.0,
        )
        try:
            while process.poll() is None:
                if time.monotonic() - started >= timeout:
                    raise subprocess.TimeoutExpired(argv, timeout)
                if os.fstat(output.fileno()).st_size + os.fstat(errors.fileno()).st_size > limit:
                    raise DiagnosticOutputLimitError("diagnostic_output_limit")
                time.sleep(0.02)
            if os.fstat(output.fileno()).st_size + os.fstat(errors.fileno()).st_size > limit:
                raise DiagnosticOutputLimitError("diagnostic_output_limit")
            output.seek(0)
            errors.seek(0)
            result = process.returncode
            if result is None:
                raise RuntimeError("diagnostic_process_exit_unavailable")
            return subprocess.CompletedProcess(argv, result, output.read(limit).decode("utf-8"), errors.read(limit).decode("utf-8"))
        finally:
            process.close()
