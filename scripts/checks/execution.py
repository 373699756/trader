from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Sequence

from scripts.checks.contracts import DiagnosticCommand, DiagnosticResult, Runner
from scripts.checks.infra.database_workspace import DatabaseWorkerEnvironment, DiagnosticOutputLimitError, PrepareDatabaseProbeDirectory, ProjectDatabaseProbeLocation, RunDatabaseProbe
from scripts.checks.projections import ResultStatus, BuildReport
from scripts.checks.projection_values import ProjectMapping

def ExecuteCommand(command: DiagnosticCommand) -> DiagnosticResult:
    started = time.monotonic()
    location = None
    try:
        argv = command.argv
        if command.database_probe_directory is not None:
            if command.runtime_config is None:
                raise ValueError("database probe requires runtime configuration")
            location = PrepareDatabaseProbeDirectory(command.database_probe_directory, command.runtime_config)
            environment = DatabaseWorkerEnvironment(location.directory)
            if command.name in {"history_write_benchmark", "database_baseline"}:
                argv += ("--disk-kind", location.disk_kind)
            completed = RunDatabaseProbe(argv, timeout=command.timeout_seconds, environment=environment)
        else:
            completed = RunDatabaseProbe(argv, timeout=command.timeout_seconds, environment=dict(os.environ))
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
    except ValueError:
        return DiagnosticResult(command.name, 2, _elapsed_ms(started), None, "database_probe_directory_invalid")
    if command.output_kind in {"text", "tool"}:
        summary: dict[str, object] = {
            "exit_code": completed.returncode,
            "output_bytes": len(completed.stdout.encode("utf-8")),
        }
        if command.output_kind == "tool":
            # Only explicitly selected engineering tools emit reviewed source/audit
            # text; supplier and runtime stdout must remain behind projections.
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
    if location is not None:
        summary = dict(ProjectMapping(decoded.get("summary")))
        summary["database_workspace"] = ProjectDatabaseProbeLocation(location)
        decoded["summary"] = summary
    return DiagnosticResult(command.name, completed.returncode, _elapsed_ms(started), decoded, None)


def _elapsed_ms(started: float) -> float:
    return round((time.monotonic() - started) * 1000.0, 1)


def RunDiagnostics(
    check_id: str,
    commands: Sequence[DiagnosticCommand],
    *,
    runner: Runner = ExecuteCommand,
) -> dict[str, object]:
    results: list[DiagnosticResult] = []
    configuration_failed = False
    cancelled = False
    for command in commands:
        name = command.name.split(":")[0]
        if cancelled or (check_id == "snapshot" and configuration_failed and name != "configuration"):
            results.append(
                DiagnosticResult(
                    command.name,
                    0,
                    0.0,
                    {"status": "skipped", "executed": False, "reason": "selection_cancelled" if cancelled else "configuration_invalid"},
                    None,
                )
            )
            continue
        result = runner(command)
        results.append(result)
        cancelled = ResultStatus(result) == "cancelled"
        if check_id == "snapshot" and name == "configuration" and ResultStatus(result) not in ("passed", "degraded"):
            configuration_failed = True
    return BuildReport(check_id, results)
