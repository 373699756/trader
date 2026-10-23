"""Opt-in CLI; no model requests occur when imported or when running tests/help."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

from .cases import Cases
from .contracts import CaseResult, ProcessResult, TraceEvidence
from .process import RunBounded
from .reports import CompareReports, ExitCode, WriteReport
from .scoring import ScoreCase
from .trace import ParseTrace
from .workspace import (
    REPOSITORY_ROOT,
    ChildEnvironment,
    ExternalPath,
    PrepareWorkspace,
    SkillDigest,
)


def _schema(path: Path) -> None:
    raw = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "summary": {"type": "string"},
            "verification": {"type": "string", "enum": ["passed", "failed", "unverified", "not_run"]},
            "live_verified": {"type": "boolean"},
            "completed": {"type": "boolean"},
        },
        "required": ["summary", "verification", "live_verified", "completed"],
    }
    path.write_text(json.dumps(raw), encoding="utf-8")


def _run(arguments: argparse.Namespace) -> int:
    destination = ExternalPath(arguments.output_dir)
    destination.mkdir(parents=True, exist_ok=False)
    destination.chmod(0o700)
    original = arguments.skill_source
    digest = SkillDigest(original)
    source = destination / "skill-inputs"
    shutil.copytree(original, source)
    harness_digest = hashlib.sha256(
        b"".join(path.read_bytes() for path in sorted(Path(__file__).parent.glob("*.py")))
    ).hexdigest()
    suite = tuple(case for case in Cases() if arguments.suite in ("all", case.name))
    signature = hashlib.sha256(repr(suite).encode()).hexdigest()
    version = subprocess.run(
        ("codex", "--version"), capture_output=True, text=True, timeout=15, check=True
    ).stdout.strip()
    schema = destination / "response.schema.json"
    _schema(schema)
    results: list[CaseResult] = []
    startup_failed = False
    expected = len(suite) * arguments.repeat
    for repetition in range(1, arguments.repeat + 1):
        for case in suite:
            case_dir = destination / f"{case.name}-{repetition}"
            case_dir.mkdir(mode=0o700)
            workspace = PrepareWorkspace(case_dir, source, case)
            command = (
                "codex",
                "exec",
                "--json",
                "--ephemeral",
                "--sandbox",
                "workspace-write",
                "-c",
                'approval_policy="never"',
                "-c",
                'web_search="disabled"',
                "-c",
                "features.multi_agent=false",
                "-c",
                "sandbox_workspace_write.network_access=false",
                "-c",
                "sandbox_workspace_write.exclude_slash_tmp=true",
                "-c",
                "sandbox_workspace_write.exclude_tmpdir_env_var=true",
                "--cd",
                str(workspace.root),
                "--add-dir",
                str(workspace.remote),
                "--add-dir",
                str(workspace.root / ".git"),
                "--output-schema",
                str(schema),
                case.prompt,
            )
            print(f"{case.name} run {repetition}/{arguments.repeat}: started", file=sys.stderr, flush=True)
            if startup_failed:
                process = ProcessResult(2, 0, "not_started_after_execution_error")
                trace = TraceEvidence(problems=("not_started",))
            else:
                process = RunBounded(command, workspace.root, case_dir, environment=ChildEnvironment())
                trace = ParseTrace(case_dir / "trace.jsonl")
            if process.exit_code != 0 and process.problem is None:
                process = ProcessResult(process.exit_code, process.elapsed_seconds, "codex_failed")
            if process.problem and not trace.commands:
                startup_failed = True
            checks = ScoreCase(case, workspace, trace)
            result = CaseResult(case.name, repetition, process, trace, checks)
            results.append(result)
            WriteReport(
                destination,
                tuple(results),
                skill_digest=digest,
                suite_digest=signature,
                cli_version=version,
                expected_runs=expected,
                harness_digest=harness_digest,
            )
            print(f"{case.name}: {result.status}", file=sys.stderr, flush=True)
    if SkillDigest(source) != digest:
        raise ValueError("skill sources changed during evaluation; rerun with a stable source")
    return ExitCode(tuple(results))


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="Explicit, isolated Codex skill evaluation (model requests on run only)"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run eight synthetic tasks against the current skills")
    run.add_argument("--suite", choices=("all", *(case.name for case in Cases())), default="all")
    run.add_argument("--repeat", type=int, choices=(1, 2, 3), default=1)
    run.add_argument("--output-dir", type=Path, required=True)
    run.add_argument(
        "--skill-source",
        type=Path,
        default=REPOSITORY_ROOT / ".agents/skills",
        help="read skill inputs from a saved snapshot instead of the checkout",
    )
    compare = commands.add_parser("compare", help="compare existing reports without a model request")
    compare.add_argument("--before", type=Path, required=True)
    compare.add_argument("--after", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        if arguments.command == "run":
            return _run(arguments)
        print(CompareReports(arguments.before, arguments.after))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError):
        print(
            "skill evaluation failed: invalid inputs, unavailable CLI or local I/O; inspect external logs",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(_main())
