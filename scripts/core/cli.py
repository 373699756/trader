"""Route one explicit tool action; unselected business modules stay unloaded."""
from __future__ import annotations

import argparse
import importlib
import json
import math
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol, cast

from scripts.core.catalog import TOOLS, WORKERS, SelectTool, ToolDefinition
from scripts.core.contracts import DiagnosticCommand, DiagnosticResult, ToolOutcome
from scripts.core.paths import PROJECT_ROOT, ExternalPath


class _ParserModule(Protocol):
    def BuildParser(self, action_identity: str, *, parents: Sequence[argparse.ArgumentParser] = ()) -> argparse.ArgumentParser: ...
    def ParseOptions(self, args: argparse.Namespace) -> object: ...


class _CommandsModule(Protocol):
    def BuildCommands(self, options: object, *, python_executable: str = sys.executable) -> tuple[DiagnosticCommand, ...]: ...


class _WorkerModule(Protocol):
    def Main(self) -> int: ...


class _ForegroundModule(Protocol):
    def Run(self, action_identity: str, arguments: list[str]) -> ToolOutcome: ...


def _common_parser(*, required: bool, add_help: bool = False) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, add_help=add_help, allow_abbrev=False)
    parser.add_argument("--business", required=required)
    parser.add_argument("--module", required=required)
    parser.add_argument("--action", required=required)
    parser.add_argument("--report", default="-", help="public report path outside the tool checkout, or -")
    parser.add_argument("--report-format", choices=("auto", "json", "text"), default="auto")
    parser.add_argument("--command-timeout-seconds", type=float, default=None)
    return parser


def BuildParser() -> argparse.ArgumentParser:
    parser = _common_parser(required=False, add_help=True)
    parser.add_argument("--list", action="store_true", help="list actions without loading business modules")
    return parser


def ParseArguments(argv: list[str]) -> tuple[ToolDefinition, argparse.Namespace]:
    selector = _common_parser(required=True)
    selected, _remaining = selector.parse_known_args(argv)
    try:
        definition = SelectTool(selected.business, selected.module, selected.action)
    except ValueError as exc:
        selector.error(str(exc))
    if definition.handler_package is not None:
        module = cast(_ParserModule, importlib.import_module(definition.handler_package + ".cli"))
        parser = module.BuildParser(definition.identity, parents=(selector,))
        parser.epilog = "Selected action effects: " + ", ".join(definition.effects) + "."
        args = parser.parse_args(argv)
    else:
        parser = selector
        args, native = parser.parse_known_args(argv)
        args.native_arguments = native[1:] if native[:1] == ["--"] else native
    args.action_identity = definition.identity
    if args.report != "-":
        raw_report = Path(args.report).expanduser()
        if raw_report.exists() or raw_report.is_symlink():
            parser.error("--report requires a new file; existing inputs and reports are never overwritten")
        existing = raw_report.parent
        while not existing.exists() and existing != existing.parent:
            existing = existing.parent
        if not existing.is_dir():
            parser.error("--report requires a directory parent")
    args.report = str(ExternalPath(Path(args.report), "--report")) if args.report != "-" else "-"
    if args.report_format == "auto":
        args.report_format = definition.stdout_report_format if args.report == "-" else "json"
    if args.report_format == "text" and not definition.supports_text:
        parser.error("the selected action does not support --report-format text")
    if args.report != "-" and (definition.artifact_output_arguments or definition.artifact_directory_arguments):
        artifact_arguments = args.native_arguments if definition.handler_package is None else argv
        for index, token in enumerate(artifact_arguments):
            name, separator, value = token.partition("=")
            if name in (*definition.artifact_output_arguments, *definition.artifact_directory_arguments):
                output = value if separator else artifact_arguments[index + 1] if index + 1 < len(artifact_arguments) else None
                if output is not None:
                    artifact = Path(output).expanduser().resolve()
                    report_path = Path(args.report)
                    if artifact == report_path or report_path.is_relative_to(artifact) or artifact.is_relative_to(report_path):
                        parser.error("the public report must be separate from the selected action's artifacts")
    if definition.execution == "foreground":
        if args.command_timeout_seconds is not None:
            parser.error("foreground maintenance actions do not accept a child-process deadline")
    else:
        if args.command_timeout_seconds is None:
            args.command_timeout_seconds = 180.0
        if not math.isfinite(args.command_timeout_seconds) or not 1 <= args.command_timeout_seconds <= 7200:
            parser.error("command deadline must be finite and within 1..7200 seconds")
    return definition, args


def _worker(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--internal-worker", choices=tuple(name for name, _ in WORKERS), required=True)
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    selected = parser.parse_args(arguments)
    tail = selected.arguments[1:] if selected.arguments[:1] == ["--"] else selected.arguments
    module = cast(_WorkerModule, importlib.import_module(selected.internal_worker))
    previous = sys.argv
    try:
        sys.argv = [selected.internal_worker, *tail]
        return int(module.Main())
    finally:
        sys.argv = previous


def _discovery(arguments: list[str]) -> int | None:
    if not arguments:
        BuildParser().print_help()
        return 0
    selected, remaining = _common_parser(required=False).parse_known_args(arguments)
    if any(flag in arguments for flag in ("--help", "-h")):
        if selected.business is not None and selected.module is not None and selected.action is not None:
            return None
        unknown = tuple(value for value in remaining if value not in {"--help", "-h", "--list"})
        if unknown:
            BuildParser().error("unrecognized arguments: " + " ".join(unknown))
        BuildParser().print_help()
        return 0
    discovery = BuildParser()
    selected, remaining = discovery.parse_known_args(arguments)
    if selected.list:
        if remaining:
            discovery.error("unrecognized arguments: " + " ".join(remaining))
        if selected.report != "-" or selected.report_format != "auto" or selected.command_timeout_seconds is not None:
            discovery.error("--list does not accept execution or report output controls")
        definitions = tuple(
            item for item in TOOLS
            if (selected.business is None or item.business == selected.business)
            and (selected.module is None or item.module == selected.module)
            and (selected.action is None or item.action == selected.action)
        )
        if not definitions:
            BuildParser().error("no actions match the requested selection")
        print(json.dumps([{
            **item.projection(),
            "execution": item.execution,
            "effects": item.effects,
        } for item in definitions], ensure_ascii=False, indent=2))
        return 0
    return None


def _foreground(definition: ToolDefinition, args: argparse.Namespace) -> tuple[int, Mapping[str, object]]:
    if definition.foreground_handler is None:
        raise ValueError("foreground handler missing from the immutable catalogue")
    handler = cast(_ForegroundModule, importlib.import_module(definition.foreground_handler))
    started = time.monotonic()
    outcome = handler.Run(definition.identity, args.native_arguments)
    from scripts.core.projections import BuildReport, ResultStatus
    result = DiagnosticResult(
        definition.identity,
        outcome.exit_code,
        (time.monotonic() - started) * 1000,
        {"status": outcome.payload.get("status"), "summary": outcome.payload,
         "schema_version": outcome.payload.get("schema_version")},
        None,
    )
    report = BuildReport(definition.identity, (result,))
    code = outcome.exit_code
    if code == 0 and (
        ResultStatus(result) not in {"passed", "degraded", "empty"}
        or report.get("status") not in {"passed", "degraded", "empty"}
    ):
        code = 1
    return code, report


def Main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["--internal-worker"]:
        return _worker(arguments)
    # Selected action help belongs to its own parser, not the global parser.
    discovery = _discovery(arguments)
    if discovery is not None:
        return discovery
    try:
        definition, args = ParseArguments(arguments)
    except ValueError as exc:
        BuildParser().error(str(exc))
    from scripts.core.execution import ExecuteCommand, RunDiagnostics
    from scripts.core.projections import ActionTextRenderer
    from scripts.core.reporting import WriteReport
    try:
        if definition.execution == "foreground":
            code, report = _foreground(definition, args)
        else:
            if definition.handler_package is not None:
                parser_module = cast(_ParserModule, importlib.import_module(definition.handler_package + ".cli"))
                command_module = cast(_CommandsModule, importlib.import_module(definition.handler_package + ".commands"))
                options = parser_module.ParseOptions(args)
                commands = command_module.BuildCommands(options)
            else:
                if definition.worker is None:
                    raise ValueError("worker missing from the immutable catalogue")
                worker_arguments = (
                    sys.executable, str(PROJECT_ROOT / "scripts/tools.py"),
                    "--internal-worker", definition.worker, "--", *args.native_arguments,
                )
                native_options = args.native_arguments[:args.native_arguments.index("--")] if "--" in args.native_arguments else args.native_arguments
                if any(argument in {"--help", "-h"} for argument in native_options):
                    result = ExecuteCommand(DiagnosticCommand(definition.identity, worker_arguments, min(args.command_timeout_seconds, 15), output_kind="tool"))
                    summary = result.payload.get("summary") if result.payload is not None else None
                    if result.return_code == 0 and isinstance(summary, Mapping) and isinstance(summary.get("tool_output"), str):
                        sys.stdout.write(summary["tool_output"])
                        return 0
                    return 1
                commands = (DiagnosticCommand(
                    definition.identity, worker_arguments, args.command_timeout_seconds,
                    output_kind=definition.worker_output_kind,
                ),)
            report = RunDiagnostics(definition.identity, commands)
            code = 0 if report.get("status") in {"passed", "degraded", "empty"} else 1
    except KeyboardInterrupt:
        code = 130
        report = {"schema_version": "trader-tools", "selection": definition.projection(), "status": "cancelled"}
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        code = 1
        report = {"schema_version": "trader-tools", "selection": definition.projection(),
                  "status": "failed", "error": type(exc).__name__}
    renderer = ActionTextRenderer(definition.identity) if args.report_format == "text" else None
    try:
        WriteReport(report, args.report, args.report_format, renderer=renderer)
    except OSError as exc:
        print(f"tool report write failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    return code
