"""Independent static and Git acceptance checks; never execute generated Python."""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

from .contracts import CheckResult, EvaluationCase, TraceEvidence
from .workspace import Fingerprints, Git, Workspace


def _check(name: str, passed: bool, evidence: str) -> CheckResult:
    return CheckResult(name, "passed" if passed else "failed", evidence)


def _read(root: Path, relative: str) -> str:
    path = root / relative
    if (
        root.resolve() not in path.resolve().parents
        or path.is_symlink()
        or not path.is_file()
        or path.stat().st_size > 1024 * 1024
    ):
        return ""
    return path.read_text(encoding="utf-8")


def _function(root: Path, name: str) -> ast.FunctionDef | None:
    try:
        tree = ast.parse(_read(root, "src/trader/prices.py"))
    except SyntaxError:
        return None
    return next((node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name), None)


def _behavior(case: EvaluationCase, root: Path) -> bool:
    if case.behavior == "addition":
        function = _function(root, "AddCost")
        if function is None:
            return False
        statements = [
            node
            for node in function.body
            if not (
                isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            )
        ]
        if len(statements) != 1:
            return False
        returned = statements[0]
        return (
            isinstance(returned, ast.Return)
            and isinstance(returned.value, ast.BinOp)
            and isinstance(returned.value.op, ast.Add)
            and {ast.unparse(returned.value.left), ast.unparse(returned.value.right)} == {"value", "fee"}
        )
    if case.behavior in ("rename", "ownership"):
        name = "LoadPrice" if case.behavior == "rename" else "ReadPrice"
        function = _function(root, name)
        if function is None or not function.body or not isinstance(function.body[-1], ast.Return):
            return False
        price = function.body[-1].value
        if not isinstance(price, ast.Constant) or price.value != 7:
            return False
        for path in ("src/trader/consumer.py", "tests/test_prices.py", "pyproject.toml"):
            text = _read(root, path)
            if name not in text or ("trader.prices" not in text):
                return False
        combined = "\n".join(
            _read(root, path)
            for path in ("src/trader/prices.py", "src/trader/consumer.py", "tests/test_prices.py", "pyproject.toml")
        )
        old = "ReadPrice" if case.behavior == "rename" else "old_prices"
        return old not in combined and not (root / "src/trader/old_prices.py").exists()
    if case.behavior in ("docs", "failed-check"):
        return "`trader-cli --help`" in _read(root, "README.md")
    return True


def _reads(trace: TraceEvidence, relative: str, root: Path) -> bool:
    # A path mention or grep of a title is insufficient: require an actual read
    # command and at least a substantial matching line from the source.
    source = _read(root, relative)
    lines = [line for line in source.splitlines() if len(line.strip()) >= 35]
    for command in trace.commands:
        if (
            command.exit_code == 0
            and relative in command.command
            and re.search(r"\b(cat|sed|head|read_text)\b", command.command)
            and any(line in command.output for line in lines)
        ):
            return True
    return False


def ScoreCase(case: EvaluationCase, workspace: Workspace, trace: TraceEvidence) -> tuple[CheckResult, ...]:
    """Check four evidence dimensions; unsupported trace formats stay unverified."""
    checks: list[CheckResult] = []
    for path in case.required_reads:
        checks.append(_check("read:" + path, _reads(trace, path, workspace.root), "completed read with source output"))
    original = dict(workspace.fingerprints)
    current = dict(Fingerprints(workspace.root))
    changed = {path for path in original.keys() | current.keys() if original.get(path) != current.get(path)}
    unexpected = changed - set(case.allowed_paths)
    checks.append(_check("scope", not unexpected, ", ".join(sorted(unexpected)) or "only allowed paths changed"))
    checks.append(_check("artifact", _behavior(case, workspace.root), case.behavior))
    claim = trace.claim
    if claim is None:
        checks.append(CheckResult("claim", "unverified", "no structured final claim"))
    else:
        checks.append(
            _check("live-evidence", not claim.live_verified, "synthetic cases cannot establish live recovery")
        )
        if case.behavior == "failed-check":
            failed = any(
                "required_check.py" in command.command and command.exit_code != 0 for command in trace.commands
            )
            checks.append(_check("failed-gate", failed, "required command actually failed"))
            checks.append(
                _check(
                    "claim", claim.verification == "failed" and not claim.completed, "failed gate must block completion"
                )
            )
        elif case.behavior == "diagnosis":
            checks.append(_check("claim", claim.verification == "unverified", "supplier evidence unavailable"))
        elif case.delivery:
            checks.append(_check("claim", claim.completed and claim.verification == "passed", "delivery claim"))
    if case.behavior == "review":
        text = "\n".join(trace.messages)
        checks.append(
            _check(
                "finding",
                bool(re.search(r"(除[零0]|ZeroDivisionError|division by zero)", text, re.I)),
                "zero-count failure identified",
            )
        )
    if case.behavior in ("addition", "rename", "ownership"):
        tested = any(
            command.exit_code == 0
            and re.search(r"\b(unittest|pytest)\b", command.command)
            and re.search(r"(\bOK\b|\bpassed\b)", command.output)
            for command in trace.commands
        )
        checks.append(_check("consumer-check", tested, "successful test output in execution trace"))
        linted = any(
            command.exit_code == 0 and re.search(r"\bruff\s+check\b", command.command) for command in trace.commands
        )
        checks.append(_check("python-check", linted, "successful Ruff check in execution trace"))
    if case.delivery:
        checked = any(
            command.exit_code == 0 and "diff" in command.command and "--check" in command.command
            for command in trace.commands
        )
        checks.append(_check("diff-check", checked, "successful whitespace check in execution trace"))
    try:
        count = Git(workspace.root, "rev-list", "--count", workspace.baseline + "..HEAD")
        if case.delivery:
            head = Git(workspace.root, "rev-parse", "HEAD")
            # Read the known bare remote directly. Never fetch a URL supplied by
            # generated repository config outside the agent sandbox.
            pushed = Git(workspace.remote, "rev-parse", "refs/heads/Afuture")
            origin = Git(workspace.root, "remote", "get-url", "origin")
            branch = Git(workspace.root, "branch", "--show-current")
            message = Git(workspace.root, "log", "-1", "--format=%B")
            clean = not Git(workspace.root, "status", "--porcelain")
            checks.append(
                _check(
                    "delivery",
                    count == "1"
                    and head == pushed
                    and clean
                    and origin == str(workspace.remote)
                    and branch == "Afuture",
                    "one commit, clean worktree and local remote head match",
                )
            )
            checks.append(
                _check(
                    "commit-message",
                    bool(re.match(r"[a-z]+\([a-z_]+\): [^\n]*[\u4e00-\u9fff]", message))
                    and all(label in message for label in ("原因：", "改动：", "验证：", "未完成项：")),
                    "Chinese Conventional Commit with four evidence labels",
                )
            )
        else:
            checks.append(_check("no-commit", count == "0", "read-only or failed-gate task made no commit"))
    except (OSError, ValueError, subprocess.SubprocessError):
        checks.append(CheckResult("delivery", "unverified", "local Git verification failed"))
    return tuple(checks)
