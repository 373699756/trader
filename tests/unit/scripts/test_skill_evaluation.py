"""Offline proofs for evidence scoring and isolation; never invoke a model."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.skill_evaluation.cases import Cases
from scripts.skill_evaluation.contracts import CommandEvidence, FinalClaim, TraceEvidence
from scripts.skill_evaluation.process import RunBounded
from scripts.skill_evaluation.reports import CompareReports, WriteReport
from scripts.skill_evaluation.scoring import ScoreCase
from scripts.skill_evaluation.trace import ParseTrace
from scripts.skill_evaluation.workspace import ExternalPath, Git, PrepareWorkspace, SkillDigest

ROOT = Path(__file__).resolve().parents[3]
SKILLS = ROOT / ".agents/skills"


def _event(kind: str, **fields: object) -> str:
    return json.dumps({"type": kind, **fields}, ensure_ascii=False) + "\n"


def _claim(*, verification: str = "passed", completed: bool = True) -> str:
    return json.dumps(
        {"summary": "检查结果", "verification": verification, "live_verified": False, "completed": completed},
        ensure_ascii=False,
    )


def _trace(path: Path, *items: str) -> TraceEvidence:
    path.write_text("".join(items), encoding="utf-8")
    return ParseTrace(path)


def test_trace_requires_completed_commands_and_structured_claim(tmp_path: Path) -> None:
    trace = _trace(
        tmp_path / "trace.jsonl",
        _event("thread.started", model="fixture-model"),
        _event("item.started", item={"type": "command_execution", "command": "pytest"}),
        _event(
            "item.completed",
            item={
                "type": "command_execution",
                "command": "python3 -m pytest",
                "aggregated_output": "1 passed",
                "exit_code": 0,
            },
        ),
        _event("item.completed", item={"type": "agent_message", "text": _claim()}),
        _event("turn.completed", usage={"input_tokens": 14, "output_tokens": 6}),
    )
    assert trace.complete and not trace.problems
    assert trace.model == "fixture-model"
    assert trace.input_tokens == 14 and trace.output_tokens == 6
    assert len(trace.commands) == 1
    assert trace.claim == FinalClaim("检查结果", "passed", False, True)


@pytest.mark.parametrize(
    "data,problem",
    [
        ('{"type":', "invalid_jsonl"),
        (_event("future.event"), "unknown_event"),
        (_event("item.completed", item={"type": "unrecognized_tool"}), "unknown_item"),
        (_event("item.completed", item={"type": "command_execution", "command": "pytest"}), "incomplete_command"),
        (_event("turn.failed"), "agent_error"),
    ],
)
def test_trace_gaps_cannot_be_scored_as_a_pass(tmp_path: Path, data: str, problem: str) -> None:
    trace = _trace(tmp_path / "trace.jsonl", data)
    assert problem in trace.problems
    assert "missing_final_claim" in trace.problems


def test_message_claiming_to_read_is_not_command_evidence(tmp_path: Path) -> None:
    case = next(case for case in Cases() if case.name == "review")
    workspace = PrepareWorkspace(tmp_path, SKILLS, case)
    trace = TraceEvidence(
        messages=("I read all SKILL.md files; division by zero",),
        claim=FinalClaim("ZeroDivisionError at prices.py:2", "not_run", False, True),
        complete=True,
    )
    scores = ScoreCase(case, workspace, trace)
    assert all(check.status == "failed" for check in scores if check.name.startswith("read:"))
    assert next(check for check in scores if check.name == "finding").status == "passed"


def test_failed_gate_rejects_false_success_claim_and_accepts_honest_failure(tmp_path: Path) -> None:
    case = next(case for case in Cases() if case.name == "failed-check")
    workspace = PrepareWorkspace(tmp_path, SKILLS, case)
    (workspace.root / "README.md").write_text("Run `trader-cli --help`.\n", encoding="utf-8")
    trace = TraceEvidence(
        commands=(CommandEvidence("python3 scripts/required_check.py", "required gate failed", 1),),
        claim=FinalClaim("全部通过", "passed", False, True),
        complete=True,
    )
    scores = ScoreCase(case, workspace, trace)
    assert next(check for check in scores if check.name == "failed-gate").status == "passed"
    assert next(check for check in scores if check.name == "claim").status == "failed"
    honest = replace(trace, claim=FinalClaim("检查失败，未完成交付", "failed", False, False))
    scores = ScoreCase(case, workspace, honest)
    assert next(check for check in scores if check.name == "claim").status == "passed"


def test_delivery_checks_actual_remote_instead_of_claim(tmp_path: Path) -> None:
    case = next(case for case in Cases() if case.name == "docs")
    workspace = PrepareWorkspace(tmp_path, SKILLS, case)
    (workspace.root / "README.md").write_text("Run `trader-cli --help`.\n", encoding="utf-8")
    read_path = case.required_reads[0]
    trace = TraceEvidence(
        commands=(
            CommandEvidence("cat " + read_path, (workspace.root / read_path).read_text(encoding="utf-8"), 0),
            CommandEvidence("git diff --check", "", 0),
        ),
        claim=FinalClaim("已推送", "passed", False, True),
        complete=True,
    )
    scores = ScoreCase(case, workspace, trace)
    assert next(check for check in scores if check.name == "delivery").status == "failed"
    Git(workspace.root, "add", "README.md")
    message = "docs(docs): 修正文档命令\n\n原因：命令缺参数。\n改动：补齐参数。\n验证：diff通过。\n未完成项：无。"
    Git(workspace.root, "commit", "-m", message)
    scores = ScoreCase(case, workspace, trace)
    assert next(check for check in scores if check.name == "delivery").status == "failed"
    Git(workspace.root, "push", "origin", "Afuture")
    assert all(check.status == "passed" for check in ScoreCase(case, workspace, trace))


@pytest.mark.parametrize("exit_code,expected", [(1, "failed"), (0, "passed")])
def test_python_validation_requires_actual_success(tmp_path: Path, exit_code: int, expected: str) -> None:
    case = Cases()[0]
    workspace = PrepareWorkspace(tmp_path, SKILLS, case)
    trace = TraceEvidence(
        commands=(CommandEvidence(".venv/bin/python3 -m ruff check src/trader/prices.py", "", exit_code),),
        claim=FinalClaim("检查通过", "passed", False, True),
        complete=True,
    )
    checks = ScoreCase(case, workspace, trace)
    assert next(check for check in checks if check.name == "python-check").status == expected


def test_workspace_has_only_synthetic_files_and_local_remote(tmp_path: Path) -> None:
    case = Cases()[0]
    original = SkillDigest(SKILLS)
    workspace = PrepareWorkspace(tmp_path, SKILLS, case)
    assert Git(workspace.root, "remote", "get-url", "origin") == str(workspace.remote)
    assert workspace.remote.is_relative_to(tmp_path)
    assert not (workspace.root / "data").exists()
    assert not (workspace.root / ".runtime").exists()
    assert SkillDigest(SKILLS) == original
    assert SkillDigest(workspace.root / ".agents/skills") == original
    ruff = subprocess.run(
        (str(workspace.root / ".venv/bin/python3"), "-m", "ruff", "--version"),
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert ruff.stdout.startswith("ruff ")


def test_output_path_rejects_checkout_relative_and_symlink_destinations(tmp_path: Path) -> None:
    for path in (ROOT / "build/evals", Path("relative-evals")):
        with pytest.raises(ValueError):
            ExternalPath(path)
    link = tmp_path / "linked"
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        ExternalPath(link / "output")
    assert ExternalPath(tmp_path / "new") == tmp_path / "new"


def test_read_only_scope_rejects_untracked_files(tmp_path: Path) -> None:
    case = next(case for case in Cases() if case.name == "review")
    workspace = PrepareWorkspace(tmp_path, SKILLS, case)
    (workspace.root / "new.py").write_text("unexpected = True\n", encoding="utf-8")
    scores = ScoreCase(case, workspace, TraceEvidence())
    assert next(check for check in scores if check.name == "scope").status == "failed"


@pytest.mark.parametrize("mode,expected", [("timeout", "timeout"), ("overflow", "output_limit")])
def test_process_limits_preserve_partial_evidence_and_reap_children(tmp_path: Path, mode: str, expected: str) -> None:
    # The child spawns a descendant that outlives its leader without group cleanup.
    program = (
        "import subprocess,sys,time; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        "print(p.pid,flush=True); "
        + ("time.sleep(60)" if mode == "timeout" else "print('x'*200000,flush=True); time.sleep(60)")
    )
    # Allow interpreter/descendant startup under parallel suite I/O before
    # testing the deadline; the fixture still sleeps far beyond this budget.
    result = RunBounded((sys.executable, "-c", program), tmp_path, tmp_path, timeout_seconds=3, output_limit=1024)
    assert result.problem == expected
    assert result.elapsed_seconds < 8
    raw = (tmp_path / "trace.jsonl").read_text(encoding="utf-8")
    assert raw and len(raw) <= 1024
    child = int(raw.splitlines()[0])
    stat = Path(f"/proc/{child}/stat")
    if stat.exists():
        assert stat.read_text().split()[2] == "Z"  # dead orphan awaiting host init reap
    else:
        with pytest.raises(ProcessLookupError):
            os.kill(child, 0)


def test_process_start_error_is_bounded(tmp_path: Path) -> None:
    result = RunBounded((str(tmp_path / "missing-cli"),), tmp_path, tmp_path)
    assert result.problem == "process_start_failed"


def test_compare_refuses_different_suites_and_incomplete_runs(tmp_path: Path) -> None:
    left, right = tmp_path / "left", tmp_path / "right"
    left.mkdir()
    right.mkdir()
    for destination in (left, right):
        WriteReport(destination, (), skill_digest="a", suite_digest="same", cli_version="fixture", expected_runs=1)
    with pytest.raises(ValueError, match="incomplete"):
        CompareReports(left, right)
    raw = json.loads((left / "summary.json").read_text())
    raw["suite_digest"] = "other"
    (left / "summary.json").write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="different"):
        CompareReports(left, right)
