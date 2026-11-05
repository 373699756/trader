"""Bounded child processes shared by tools; no business imports."""
from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
from collections.abc import Mapping

from scripts.core.paths import PROJECT_ROOT


class DiagnosticOutputLimitError(ValueError):
    """Captured child output exceeded the shared tool limit."""


def RunBoundedCommand(
    argv: tuple[str, ...], *, timeout: float, environment: Mapping[str, str]
) -> subprocess.CompletedProcess[str]:
    """Bound execution/output; allow five extra seconds for owned worker cleanup on POSIX."""
    limit = 20 * 1024 * 1024
    started = time.monotonic()
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors, subprocess.Popen(
        argv,
        cwd=PROJECT_ROOT,
        stdout=output,
        stderr=errors,
        env=environment,
        start_new_session=os.name == "posix",
    ) as process:
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
            return subprocess.CompletedProcess(argv, process.returncode, output.read(limit).decode("utf-8"), errors.read(limit).decode("utf-8"))
        finally:
            try:
                if os.name == "posix":
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGINT)
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            pass
                    os.killpg(process.pid, signal.SIGKILL)
                elif process.poll() is None:
                    process.kill()
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
