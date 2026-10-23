"""Bounded subprocess lifetime, including descendants and combined output."""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time
from pathlib import Path

from .contracts import ProcessResult


def _stop(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    # The leader may have exited while a descendant is still holding its pipes.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=2)


def RunBounded(
    command: tuple[str, ...],
    cwd: Path,
    destination: Path,
    *,
    timeout_seconds: float = 600,
    output_limit: int = 20 * 1024 * 1024,
    environment: dict[str, str] | None = None,
) -> ProcessResult:
    """Stream stdout/stderr to an exclusive external directory, never into RAM."""
    if not 0 < timeout_seconds <= 600 or not 0 < output_limit <= 20 * 1024 * 1024:
        raise ValueError("invalid process limits")
    started = time.monotonic()
    problem = None
    total = 0
    with (destination / "trace.jsonl").open("xb") as stdout, (destination / "stderr.log").open("xb") as stderr:
        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError:
            return ProcessResult(2, time.monotonic() - started, "process_start_failed")
        assert process.stdout is not None and process.stderr is not None
        with process.stdout, process.stderr, selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, stdout)
            selector.register(process.stderr, selectors.EVENT_READ, stderr)
            try:
                while selector.get_map():
                    if time.monotonic() - started >= timeout_seconds:
                        problem = "timeout"
                        break
                    for key, _ in selector.select(timeout=min(0.1, timeout_seconds)):
                        block = os.read(key.fd, 65536)
                        if not block:
                            selector.unregister(key.fileobj)
                            continue
                        remaining = max(0, output_limit - total)
                        key.data.write(block[:remaining])
                        total += len(block)
                        if total >= output_limit:
                            problem = "output_limit"
                            break
                    if problem:
                        break
                if problem:
                    _stop(process)
                else:
                    try:
                        process.wait(timeout=max(0.01, timeout_seconds - (time.monotonic() - started)))
                    except subprocess.TimeoutExpired:
                        problem = "timeout"
                        _stop(process)
            finally:
                _stop(process)
        return ProcessResult(process.returncode, round(time.monotonic() - started, 3), problem)
