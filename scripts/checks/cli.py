"""Public diagnostic orchestration; unselected workers are never imported."""

from __future__ import annotations

import argparse
import importlib
import json
import math
import sys
from pathlib import Path

from scripts.checks.catalog import CHECKS, WORKERS, SelectCheck


def _worker(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--internal-worker", choices=tuple(name for name, _ in WORKERS), required=True)
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    selected = parser.parse_args(arguments)
    tail = selected.arguments[1:] if selected.arguments[:1] == ["--"] else selected.arguments
    worker = importlib.import_module(selected.internal_worker)
    previous = sys.argv
    try:
        sys.argv = [selected.internal_worker, *tail]
        return int(worker.Main())
    finally:
        sys.argv = previous


def Main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["--internal-worker"]:
        return _worker(arguments)
    if arguments == ["--list"]:
        print(json.dumps([item.projection() for item in CHECKS], ensure_ascii=False, indent=2))
        return 0

    from scripts.checks.arguments import ExternalPath, BuildParser, ValidateOptions

    parser = BuildParser()
    public, separator, native = arguments, False, []
    if "--" in arguments:
        index = arguments.index("--")
        public, separator, native = arguments[:index], True, arguments[index + 1:]
    args = parser.parse_args(public)
    try:
        definition = SelectCheck(args.business, args.module, args.check)
    except ValueError as exc:
        parser.error(str(exc))
    from scripts.checks.commands import BuildCommands
    from scripts.checks.contracts import DiagnosticCommand
    from scripts.checks.execution import ExecuteCommand, RunDiagnostics
    from scripts.checks.projections import BuildReport
    from scripts.checks.reporting import WriteReport

    output, output_format = "-", "json"
    commands: tuple[DiagnosticCommand, ...]
    try:
        if definition.worker is not None:
            if not math.isfinite(args.command_timeout_seconds) or not 1 <= args.command_timeout_seconds <= 7200:
                raise ValueError("invalid command deadline")
            if args.output != "-":
                output = str(ExternalPath(Path(args.output), "--output"))
            worker_arguments = (
                sys.executable, "-m", "scripts.diagnose_runtime", "--internal-worker", definition.worker, "--", *native,
            )
            if native == ["--help"]:
                # Parser help is an explicit discovery action, not a diagnostic
                # result. Keep it readable and bounded without executing a check.
                result = ExecuteCommand(DiagnosticCommand(
                    definition.identity, worker_arguments, min(args.command_timeout_seconds, 15), output_kind="tool",
                ))
                if result.return_code == 0 and result.payload is not None:
                    summary = result.payload["summary"]
                    if isinstance(summary, dict) and isinstance(summary.get("tool_output"), str):
                        sys.stdout.write(summary["tool_output"])
                        return 0
                WriteReport(BuildReport(definition.identity, (result,)), output)
                return 1
            commands = (DiagnosticCommand(
                definition.identity,
                worker_arguments,
                args.command_timeout_seconds,
                output_kind="tool" if definition.identity in {"quality-transform", "skill-evaluation"}
                else "text" if definition.identity == "quality-debt" else "json",
            ),)
        else:
            if separator:
                raise ValueError("native arguments are supported only for explicitly selected engineering/resource workers")
            options, output = ValidateOptions(args)
            output_format = options.output_format
            if options.cpu_profile_dir is not None:
                options.cpu_profile_dir.mkdir(parents=True, exist_ok=True)
            commands = BuildCommands(options)
        report = RunDiagnostics(definition.identity, commands)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        report = {
            "schema_version": "trader-runtime-diagnostics",
            "selection": definition.projection(),
            "status": "failed",
            "error": type(exc).__name__,
        }
    WriteReport(report, output, output_format)
    return 0 if report.get("status") in {"passed", "degraded", "empty"} else 1
