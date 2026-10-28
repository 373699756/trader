"""Disposable repositories, with no production files or original remote copied."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys
import venv
from dataclasses import dataclass
from pathlib import Path

from .contracts import EvaluationCase

SKILL_NAMES = ("trader-delivery", "python-engineering-guide")
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

_AGENTS = """# Isolated Trader skill fixture

This is a synthetic repository. No market data or production services exist here.
Use the repository skills when applicable. Read AGENTS.md and pyproject.toml first.
Planning and read-only requests do not authorize edits, commits or pushes.
For repository changes use $trader-delivery; Python changes use $python-engineering-guide.
Naming authority: [.agents coding rules](.agents/skills/python-engineering-guide/references/coding-standard.md).
Current branch is Afuture. Keep unrelated files unchanged. One responsibility has one owner;
switch all consumers atomically and delete the old path, with no compatibility aliases.
All manual edits use apply_patch. Direct Python commands use python3 or .venv/bin/python3.
For these small tasks do not create a ledger entry. Runtime business authorities, if needed,
are docs/1_评分逻辑.md, docs/2_工程设计.md and docs/4_下载训练.md.
For Python behavior changes run PYTHONPATH=src python3 -m unittest discover -s tests -q.
For documentation only use full diff review and git diff --check, with no new tests.
Delivery requires exact staged scope, one Conventional Commit with a Chinese title and
原因、改动、验证、未完成项 in its body, a local origin push and upstream confirmation.
The title must be <type>(<scope>): <Chinese change>, with scope docs or tooling
for these synthetic tasks. Check the staged diff and the actual message before committing.
Ruff is installed in the disposable venv: use .venv/bin/python3 -m ruff check
on changed Python paths; the system python3 does not include development packages.
Only the local temporary origin is authorized. Keep failed or unrun checks explicit.
Do not claim recovery from HTTP 200 or unavailable live evidence.
"""
_PROJECT = """[project]
name = "trader-skill-fixture"
version = "0.0.0"
requires-python = ">=3.10,<3.15"
[project.scripts]
fixture-price = "trader.prices:ReadPrice"
"""


@dataclass(frozen=True)
class Workspace:
    root: Path
    remote: Path
    baseline: str
    fingerprints: tuple[tuple[str, str], ...]


def ExternalPath(path: Path) -> Path:
    """Require an explicit absolute destination outside this checkout, without symlinks."""
    if not path.is_absolute():
        raise ValueError("output directory must be absolute and outside the repository")
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError("symlink destinations are not allowed")
    resolved = path.resolve()
    if resolved == REPOSITORY_ROOT or REPOSITORY_ROOT in resolved.parents:
        raise ValueError("output directory must be outside the repository")
    return resolved


def Git(root: Path, *arguments: str) -> str:
    """Run bounded local Git plumbing without shell evaluation or inherited Git paths."""
    if root.is_symlink() or (root / ".git").is_symlink():
        raise ValueError("Git evaluation paths must not be symlinks")
    environment = ChildEnvironment()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    result = subprocess.run(
        (
            "git",
            "--no-pager",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "protocol.allow=never",
            "-c",
            "protocol.file.allow=always",
            *arguments,
        ),
        cwd=root,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return result.stdout.strip()


def ChildEnvironment() -> dict[str, str]:
    """Retain Codex authentication/config selection; remove checkout-specific execution state."""
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_") and key not in ("PYTHONPATH", "VIRTUAL_ENV", "PWD", "OLDPWD")
    }


def Fingerprints(root: Path) -> tuple[tuple[str, str], ...]:
    """Hash bounded regular files only; symlinks are represented without following them."""
    values = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in (".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache") for part in relative.parts):
            continue
        if path.is_symlink():
            digest = "symlink"
        elif path.is_file():
            if path.stat().st_size > 1024 * 1024:
                digest = "oversized"
            else:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            continue
        values.append((relative.as_posix(), digest))
        if len(values) > 1000:
            raise ValueError("fixture file inventory exceeded 1000 files")
    return tuple(values)


def SkillDigest(source: Path) -> str:
    """Bind the run to exact skill files, including supporting references and metadata."""
    digest = hashlib.sha256()
    for name in SKILL_NAMES:
        for path in sorted((source / name).rglob("*")):
            if path.is_symlink():
                raise ValueError("skill sources must not contain symlinks")
            if path.is_file():
                digest.update(path.relative_to(source).as_posix().encode())
                digest.update(b"\0")
                digest.update(path.read_bytes())
    return digest.hexdigest()


def PrepareWorkspace(destination: Path, source: Path, case: EvaluationCase) -> Workspace:
    """Create one independent branch and local bare remote under the external destination."""
    root = destination / "workspace"
    root.mkdir()
    skills = root / ".agents/skills"
    skills.mkdir(parents=True)
    for name in SKILL_NAMES:
        shutil.copytree(source / name, skills / name)
    content = {
        "AGENTS.md": _AGENTS,
        "pyproject.toml": _PROJECT,
        ".gitignore": "__pycache__/\n.venv/\n.pytest_cache/\n.ruff_cache/\n",
        "README.md": "# Fixture\n\nRun `trader-cli`.\n",
        "src/trader/__init__.py": "",
        "tests/__init__.py": "",
        "docs/3_工程实施.md": "# Engineering ledger\n\nNo active stage.\n",
        "docs/README.md": "# Authorities\n\nSee the numbered documents.\n",
        "docs/1_评分逻辑.md": "# Scoring\n\nNo scoring behavior is exercised in this fixture.\n",
        "docs/2_工程设计.md": "# Engineering\n\nRead-only delivery; one owner; no compatibility aliases.\n",
        "docs/4_下载训练.md": "# Data\n\nNo active databases or suppliers are available.\n",
    }
    content.update((file.path, file.content) for file in case.files)
    if case.behavior == "ownership":
        content["pyproject.toml"] = _PROJECT.replace("trader.prices", "trader.old_prices")
    for name, text in content.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    # A bare interpreter symlink loses the caller's site-packages. Provide only
    # Ruff in a real disposable venv, never an editable product install or a .pth
    # pointing at the original checkout.
    venv.EnvBuilder(with_pip=False).create(root / ".venv")
    ruff = importlib.util.find_spec("ruff")
    if ruff is None or ruff.origin is None:
        raise ValueError("evaluation requires the repository's installed Ruff")
    site = root / ".venv/lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    shutil.copytree(Path(ruff.origin).parent, site / "ruff", ignore=shutil.ignore_patterns("__pycache__"))
    binary = Path(sys.executable).parent / "ruff"
    if not binary.is_file():
        raise ValueError("evaluation requires the repository's Ruff binary")
    shutil.copy2(binary, root / ".venv/bin/ruff")
    Git(root, "init", "--initial-branch=Afuture")
    Git(root, "config", "user.name", "Skill Evaluation")
    Git(root, "config", "user.email", "skill-evaluation@example.invalid")
    Git(root, "add", ".")
    Git(root, "commit", "-m", "test(tooling): 初始化隔离案例")
    remote = destination / "origin.git"
    Git(root, "init", "--bare", str(remote))
    Git(root, "remote", "add", "origin", str(remote))
    Git(root, "push", "--set-upstream", "origin", "Afuture")
    return Workspace(root, remote, Git(root, "rev-parse", "HEAD"), Fingerprints(root))
