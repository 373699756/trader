"""Explicit external database workspaces and truthful filesystem evidence."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from trader.infra.settings import LoadRuntimeSettings
from trader.infra.disk_probe import DiskProbe
from trader.infra.disk_types import DiskKind

from scripts.checks.contracts import PROJECT_ROOT


@dataclass(frozen=True)
class DatabaseProbeLocation:
    directory: Path
    disk_kind: DiskKind
    disk_source: str
    filesystem: str
    memory_backed: bool


class DiagnosticOutputLimitError(ValueError):
    """Captured child output exceeded the shared diagnostic limit."""


def PrepareDatabaseProbeDirectory(directory: Path, runtime_config: Path) -> DatabaseProbeLocation:
    """Create only a new external workspace, after validating configuration and location."""
    if not directory.is_absolute():
        raise ValueError("database probe directory must be absolute")
    target = directory.resolve()
    settings = LoadRuntimeSettings(runtime_config)
    protected = tuple(
        path.resolve() for path in (PROJECT_ROOT, settings.project_root, settings.data_paths.root, settings.runtime_dir)
    )
    if any(target == path or target.is_relative_to(path) or path.is_relative_to(target) for path in protected):
        raise ValueError("database probe directory must be outside project and active data")
    if directory.is_symlink() or target.exists():
        raise ValueError("database probe directory must be new")
    existing = target.parent
    while not existing.exists():
        existing = existing.parent
    filesystem = _filesystem(existing)
    memory = filesystem in {"tmpfs", "ramfs"}
    detected = DiskProbe().inspect(target)
    kind = detected.kind
    source: str = detected.source
    # External probe media must be measured independently from the application's configured medium.
    if memory:
        kind, source = "unknown", "memory_filesystem"
    target.mkdir(parents=True, exist_ok=False)
    return DatabaseProbeLocation(target, kind, source, filesystem, memory)


def ProjectDatabaseProbeLocation(location: DatabaseProbeLocation) -> dict[str, object]:
    return {
        "disk_kind": location.disk_kind,
        "disk_source": location.disk_source,
        "filesystem": location.filesystem,
        "memory_backed": location.memory_backed,
        "disk_performance_evidence": not location.memory_backed
        and location.filesystem != "unknown"
        and location.disk_kind != "unknown",
    }


def _filesystem(path: Path) -> str:
    if not sys.platform.startswith("linux"):
        return "unknown"
    selected, depth = "unknown", -1
    try:
        lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError:
        return selected
    for line in lines:
        before, separator, after = line.partition(" - ")
        fields = before.split()
        if not separator or len(fields) < 5 or not after.split():
            continue
        mount = Path(re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), fields[4]))
        if path.is_relative_to(mount) and len(mount.parts) >= depth:
            selected, depth = after.split()[0], len(mount.parts)
    return selected


def DatabaseWorkerEnvironment(directory: Path) -> dict[str, str]:
    """Subprocess-local tempfile root, including an older baseline's unchanged code."""
    return {**os.environ, "TMPDIR": str(directory), "TEMP": str(directory), "TMP": str(directory)}


def RunDatabaseProbe(
    argv: tuple[str, ...], *, timeout: float, environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Bound child lifetime and captured output; always reap owned descendants on POSIX."""
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
                    os.killpg(process.pid, signal.SIGKILL)
                elif process.poll() is None:
                    process.kill()
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
