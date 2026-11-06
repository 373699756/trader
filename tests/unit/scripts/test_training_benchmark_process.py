"""Owned cancellation reaches escaped sessions and preserves unrelated processes."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from scripts.training.jobs import benchmark_process
from scripts.training.jobs.benchmark_process import StopTrainingBenchmarkProcessTree
from scripts.training.jobs.benchmark_process import RunTrainingBenchmarkProcess

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux proc is required")

_STUBBORN = "import signal,time;signal.signal(signal.SIGINT,signal.SIG_IGN);signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)"
_CHILD = """
import json,os,signal,subprocess,sys,time
from pathlib import Path
signal.signal(signal.SIGINT,signal.SIG_IGN)
signal.signal(signal.SIGTERM,signal.SIG_IGN)
leaf=subprocess.Popen([sys.executable,'-c',sys.argv[2]],start_new_session=True)
Path(sys.argv[1]).write_text(json.dumps([os.getppid(),os.getpid(),leaf.pid]))
time.sleep(60)
"""
_ROOT = """
import signal,subprocess,sys,time
signal.signal(signal.SIGINT,signal.SIG_IGN)
signal.signal(signal.SIGTERM,signal.SIG_IGN)
subprocess.Popen([sys.executable,'-c',sys.argv[2],sys.argv[1],sys.argv[3]],start_new_session=True)
time.sleep(60)
"""


def _spawn(program: str, *arguments: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        (sys.executable, "-c", program, *arguments),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _ready(path: Path) -> list[int]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if path.exists():
            try:
                value: object = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            assert isinstance(value, list) and all(type(pid) is int for pid in value)
            return value
        time.sleep(0.01)
    raise TimeoutError("fixture_process_did_not_become_ready")


def _running(pid: int) -> bool:
    process = benchmark_process._read_process(pid)
    return process is not None and process.running


def _cleanup(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.kill()
    process.wait(timeout=5)


def test_actual_tree_and_two_escaped_sessions_stop_while_unrelated_sentinel_survives(tmp_path: Path) -> None:
    ready = tmp_path / "tree.json"
    root = _spawn(_ROOT, str(ready), _CHILD, _STUBBORN)
    sentinel = _spawn(_STUBBORN)
    pids: list[int] = []
    try:
        pids = _ready(ready)
        assert pids[0] == root.pid and len(set(pids)) == 3
        assert all(_running(pid) for pid in pids)
        assert os.getsid(pids[1]) == pids[1] and os.getsid(pids[2]) == pids[2]
        started = time.monotonic()
        ended = StopTrainingBenchmarkProcessTree(root.pid, timeout_seconds=1.0)
        assert set(ended) == set(pids)
        assert all(not _running(pid) for pid in pids)
        assert sentinel.poll() is None and _running(sentinel.pid)
        assert time.monotonic() - started < 1.5
        root.wait(timeout=5)
    finally:
        for pid in pids:
            if _running(pid):
                os.kill(pid, signal.SIGKILL)
        _cleanup(root)
        _cleanup(sentinel)


def test_cooperative_root_gets_interrupt_before_termination(tmp_path: Path) -> None:
    ready = tmp_path / "ready.json"
    interrupted = tmp_path / "interrupted"
    program = """
import json,os,signal,sys,time
from pathlib import Path
def stop(signum,frame):
 Path(sys.argv[2]).write_text('SIGINT')
 raise SystemExit(0)
signal.signal(signal.SIGINT,stop)
Path(sys.argv[1]).write_text(json.dumps([os.getpid()]))
time.sleep(60)
"""
    root = _spawn(program, str(ready), str(interrupted))
    try:
        _ready(ready)
        assert StopTrainingBenchmarkProcessTree(root.pid, timeout_seconds=1.0) == (root.pid,)
        assert interrupted.read_text(encoding="utf-8") == "SIGINT"
        assert root.wait(timeout=5) == 0
    finally:
        _cleanup(root)


@pytest.mark.parametrize("pid", (-1, 0, 1, True, 1.5, "123"))
def test_invalid_root_is_rejected_before_any_signal(pid: object) -> None:
    with pytest.raises(ValueError, match="training_benchmark_process_root_invalid"):
        StopTrainingBenchmarkProcessTree(cast(int, pid))


@pytest.mark.parametrize("pid", (os.getpid(), os.getppid()))
def test_calling_process_and_ancestor_are_never_stopped(pid: int) -> None:
    with pytest.raises(ValueError, match="training_benchmark_process_root_(invalid|is_ancestor)"):
        StopTrainingBenchmarkProcessTree(pid)


@pytest.mark.parametrize("timeout", (0.0, -1.0, 30.1, float("inf"), float("nan"), True, "1", 10**400))
def test_timeout_is_finite_positive_and_capped_at_thirty_seconds(timeout: object) -> None:
    with pytest.raises(ValueError, match="training_benchmark_process_timeout_invalid"):
        StopTrainingBenchmarkProcessTree(2**30, timeout_seconds=cast(float, timeout))


def test_exited_root_and_missing_pid_are_safe_no_ops() -> None:
    root = _spawn("pass")
    root.wait(timeout=5)
    assert StopTrainingBenchmarkProcessTree(root.pid) == ()
    assert StopTrainingBenchmarkProcessTree(2**30) == ()


def test_reused_birth_identity_is_not_admitted_or_signalled(monkeypatch: pytest.MonkeyPatch) -> None:
    root = _spawn(_STUBBORN)
    original = benchmark_process._read_process
    reads = 0

    def changed_birth(pid: int) -> benchmark_process._Process | None:
        nonlocal reads
        process = original(pid)
        if pid == root.pid and process is not None:
            reads += 1
            if reads > 1:
                return replace(process, identity=replace(process.identity, birth_ticks=process.identity.birth_ticks + 1))
        return process

    monkeypatch.setattr(benchmark_process, "_read_process", changed_birth)
    try:
        assert StopTrainingBenchmarkProcessTree(root.pid, timeout_seconds=0.2) == ()
        assert root.poll() is None
    finally:
        _cleanup(root)


def test_recorded_birth_is_checked_again_before_escalation_signal() -> None:
    root = _spawn(_STUBBORN)
    try:
        current = benchmark_process._read_process(root.pid)
        assert current is not None
        old_identity = replace(current.identity, birth_ticks=current.identity.birth_ticks - 1)
        benchmark_process._signal_owned({root.pid: old_identity}, signal.SIGKILL)
        assert root.poll() is None
    finally:
        _cleanup(root)


def test_cleanup_failure_is_explicit_and_does_not_extend_the_wait_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    root = _spawn(_STUBBORN)

    def no_signal(owned: dict[int, benchmark_process._ProcessIdentity], signum: signal.Signals) -> None:
        return None

    monkeypatch.setattr(benchmark_process, "_signal_owned", no_signal)
    try:
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="training_benchmark_process_cleanup_incomplete"):
            StopTrainingBenchmarkProcessTree(root.pid, timeout_seconds=0.2)
        assert time.monotonic() - started < 0.7
        assert root.poll() is None
    finally:
        _cleanup(root)


def test_supervision_keeps_actual_exit_and_both_logs(tmp_path: Path) -> None:
    result = RunTrainingBenchmarkProcess(
        (sys.executable, "-c", "import sys;print('result');print('progress',file=sys.stderr);sys.exit(7)"),
        cwd=tmp_path, stdout=tmp_path / "stdout", stderr=tmp_path / "stderr",
        timeout_seconds=5, minimum_free_bytes=1,
    )
    assert result.exit_code == 7 and result.reason == "exited"
    assert (tmp_path / "stdout").read_text().strip() == "result"
    assert (tmp_path / "stderr").read_text().strip() == "progress"


@pytest.mark.parametrize("cause", ("timeout", "low_space", "output_limit"))
def test_supervision_stops_owned_workload_and_preserves_unrelated_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cause: str,
) -> None:
    usage = benchmark_process.shutil.disk_usage(tmp_path)
    calls = 0

    def space(path: Path) -> object:
        nonlocal calls
        calls += 1
        return usage._replace(free=0 if cause == "low_space" and calls > 2 else usage.free)

    monkeypatch.setattr(benchmark_process.shutil, "disk_usage", space)
    sentinel = _spawn(_STUBBORN)
    program = "import os,time;print(os.getpid(),flush=True);time.sleep(60)"
    try:
        result = RunTrainingBenchmarkProcess(
            (sys.executable, "-c", program), cwd=tmp_path,
            stdout=tmp_path / "stdout", stderr=tmp_path / "stderr",
            timeout_seconds=0.3 if cause == "timeout" else 5, minimum_free_bytes=1,
            maximum_output_bytes=1 if cause == "output_limit" else 1024,
        )
        assert result.reason == cause
        pid = int((tmp_path / "stdout").read_text())
        assert not _running(pid) and sentinel.poll() is None
    finally:
        _cleanup(sentinel)


def test_supervision_rejects_low_space_before_starting_work(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="training_benchmark_supervision_space_insufficient"):
        RunTrainingBenchmarkProcess(
            (sys.executable, "-c", "raise SystemExit('must not run')"), cwd=tmp_path,
            stdout=tmp_path / "stdout", stderr=tmp_path / "stderr",
            timeout_seconds=5, minimum_free_bytes=10**30,
        )
    assert not (tmp_path / "stdout").exists()


def test_supervision_observation_failure_reaps_workload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    usage = benchmark_process.shutil.disk_usage(tmp_path)
    calls = 0

    def failed_space(path: Path) -> object:
        nonlocal calls
        calls += 1
        if calls > 2:
            raise OSError("observation_failed")
        return usage

    monkeypatch.setattr(benchmark_process.shutil, "disk_usage", failed_space)
    with pytest.raises(OSError, match="observation_failed"):
        RunTrainingBenchmarkProcess(
            (sys.executable, "-c", "import os,time;print(os.getpid(),flush=True);time.sleep(60)"),
            cwd=tmp_path, stdout=tmp_path / "stdout", stderr=tmp_path / "stderr",
            timeout_seconds=5, minimum_free_bytes=1,
        )
    assert not _running(int((tmp_path / "stdout").read_text()))


def test_supervision_refuses_existing_logs_before_creating_another_file(tmp_path: Path) -> None:
    (tmp_path / "stderr").write_text("original", encoding="utf-8")
    with pytest.raises(ValueError, match="training_benchmark_supervision_arguments_invalid"):
        RunTrainingBenchmarkProcess(
            (sys.executable, "-c", "pass"), cwd=tmp_path,
            stdout=tmp_path / "stdout", stderr=tmp_path / "stderr",
            timeout_seconds=5, minimum_free_bytes=1,
        )
    assert not (tmp_path / "stdout").exists()
    assert (tmp_path / "stderr").read_text() == "original"
