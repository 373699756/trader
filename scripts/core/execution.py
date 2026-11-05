"""Execute selected commands with explicit preparation and dependency contracts."""
from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Sequence

from scripts.core.contracts import CommandPreparationError, DiagnosticCommand, DiagnosticResult, Runner
from scripts.core.process import DiagnosticOutputLimitError, RunBoundedCommand
from scripts.core.projections import BuildReport, NormalizeActionResult, ResultStatus
from scripts.core.projection_values import ProjectMapping


def ExecuteCommand(command: DiagnosticCommand) -> DiagnosticResult:
    started = time.monotonic()
    metadata = None
    try:
        preparation = command.prepare() if command.prepare is not None else None
        environment = preparation.environment if preparation is not None and preparation.environment is not None else dict(os.environ)
        argv = command.argv + (preparation.argv_suffix if preparation is not None else ())
        metadata = preparation.report_metadata if preparation is not None else None
        completed = RunBoundedCommand(argv, timeout=command.timeout_seconds, environment=environment)
    except KeyboardInterrupt:
        return DiagnosticResult(command.name, 130, _elapsed_ms(started), None, "command_cancelled")
    except subprocess.TimeoutExpired:
        return DiagnosticResult(command.name, 124, _elapsed_ms(started), None, "command_timeout")
    except OSError:
        return DiagnosticResult(command.name, 126, _elapsed_ms(started), None, "command_launch_failed")
    except UnicodeError:
        return DiagnosticResult(command.name, 1, _elapsed_ms(started), None, "invalid_output_encoding")
    except DiagnosticOutputLimitError:
        return DiagnosticResult(command.name, 1, _elapsed_ms(started), None, "command_output_limit")
    except CommandPreparationError as exc:
        return DiagnosticResult(command.name, 2, _elapsed_ms(started), None, exc.error_code)
    if command.output_kind in {"text", "tool"}:
        summary: dict[str, object] = {
            "exit_code": completed.returncode,
            "output_bytes": len(completed.stdout.encode("utf-8")),
        }
        if command.output_kind == "tool":
            summary["tool_output"] = completed.stdout
        return DiagnosticResult(command.name, completed.returncode, _elapsed_ms(started), {
            "status": "passed" if completed.returncode == 0 else "failed",
            "summary": summary,
        }, None)
    try:
        decoded = json.loads(completed.stdout)
    except (UnicodeError, json.JSONDecodeError):
        return DiagnosticResult(command.name, completed.returncode, _elapsed_ms(started), None, "invalid_json")
    if not isinstance(decoded, dict) or any(not isinstance(key, str) for key in decoded):
        return DiagnosticResult(command.name, completed.returncode, _elapsed_ms(started), None, "invalid_json_root")
    if metadata is not None:
        decoded["summary"] = {**ProjectMapping(decoded.get("summary")), **metadata}
    return DiagnosticResult(command.name, completed.returncode, _elapsed_ms(started), decoded, None)


def _elapsed_ms(started: float) -> float:
    return round((time.monotonic() - started) * 1000.0, 1)


def RunDiagnostics(
    action_id: str,
    commands: Sequence[DiagnosticCommand],
    *,
    runner: Runner = ExecuteCommand,
) -> dict[str, object]:
    results: list[DiagnosticResult] = []
    cancelled = False
    for command in commands:
        ready = all(
            any(result.name == dependency and ResultStatus(result) in {"passed", "degraded"} for result in results)
            for dependency in command.requires_success
        )
        if cancelled or not ready:
            results.append(DiagnosticResult(
                command.name, 0, 0.0,
                {"status": "skipped", "executed": False,
                 "reason": "selection_cancelled" if cancelled else "dependency_invalid"},
                None,
            ))
            continue
        result = NormalizeActionResult(action_id, runner(command))
        results.append(result)
        cancelled = ResultStatus(result) == "cancelled"
    return BuildReport(action_id, results)
