"""Explicit opt-in commands; the existing no-argument gate is unchanged."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .files import ROOTS, Change, Diff, ReadSources, SafePath, SourceFile, WriteChanges
from .formatting import FormatSource
from .manifest import ParseManifest
from .naming import Audit

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _git_head(root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, timeout=15, check=True
    )
    return result.stdout.strip()


def _require_versions(*names: str) -> None:
    configuration = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for name in names:
        pins = re.findall(r'"' + re.escape(name) + r'==([^"\s]+)"', configuration)
        if len(pins) != 1 or version(name) != pins[0]:
            raise ValueError(f"{name} must match its unique pyproject.toml version pin")


def _renames(root: Path, sources: tuple[SourceFile, ...], manifest_text: str) -> tuple[Change, ...]:
    manifest = ParseManifest(manifest_text)
    if manifest.base_commit != _git_head(root):
        raise ValueError("manifest base_commit differs from HEAD")
    if {(item.path, item.sha256) for item in manifest.files} != {(item.path, item.digest) for item in sources}:
        raise ValueError("manifest must bind the entire current Python source inventory and content hashes")
    for item in manifest.renames:
        SafePath(root, item.path)
        module = item.path.removesuffix(".py").removeprefix("src/").replace("/", ".").removesuffix(".__init__")
        symbol = ".".join(part for part in (item.scope, item.old) if part)
        # Entry-point metadata is outside the Python-only transformation boundary.
        metadata = root / "pyproject.toml"
        if metadata.is_file() and any(
            reference in metadata.read_text(encoding="utf-8")
            for reference in (f"{module}:{symbol}", f"{module}.{symbol}")
        ):
            raise ValueError("pyproject entry-point reference requires a separate reviewed metadata migration")
    for reference in manifest.references:
        SafePath(root, reference.path)
    with tempfile.TemporaryDirectory(prefix="trader-rename-") as directory:
        temporary = Path(directory)
        for folder in ("src", "scripts", "tests"):
            (temporary / folder).mkdir()
        for source in sources:
            target = temporary / source.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.content)
        manifest_path = temporary / "rename.json"
        manifest_path.write_text(manifest_text, encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-m", "scripts.code_quality.rename_worker", str(temporary), str(manifest_path)],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=300,
            check=False,
        )
        if result.returncode:
            raise ValueError(f"rename analysis refused: {result.stderr.strip()}")
        output = json.loads(result.stdout)
        if not isinstance(output, dict) or set(output) != {source.path for source in sources}:
            raise ValueError("invalid rename worker output")
        changes: list[Change] = []
        for source in sources:
            text = output[source.path]
            if not isinstance(text, str):
                raise ValueError("invalid renamed source")
            if "\r\n" in source.text:
                if "\n" in source.text.replace("\r\n", ""):
                    raise ValueError(f"mixed line endings require separate formatting: {source.path}")
                text = text.replace("\r\n", "\n").replace("\n", "\r\n")
            if text != source.text:
                if source.path.startswith("src/trader/") and len(text.splitlines()) > 1500:
                    raise ValueError(f"renamed source exceeds 1500 lines: {source.path}")
                changes.append(Change(source, text))
        return tuple(changes)


def Main(argv: list[str] | None = None, *, root: Path = PROJECT_ROOT) -> int:
    parser = argparse.ArgumentParser(
        description="Opt-in formatting and reviewed naming; never writes without write mode."
    )
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--format", choices=("check", "preview", "write"))
    modes.add_argument("--naming-audit", action="store_true")
    modes.add_argument("--rename", choices=("check", "preview", "write"))
    parser.add_argument("--all", action="store_true", dest="all_sources")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--json", action="store_true", help="audit findings plus a content-bound manifest template")
    parser.add_argument("paths", nargs="*")
    args = parser.parse_args(argv)
    try:
        if args.rename:
            if args.manifest is None or args.paths or args.all_sources or args.json:
                raise ValueError("rename requires only --manifest (consumer scope is always all Python sources)")
            _require_versions("rope")
            sources = ReadSources(root, tuple(base for base in ROOTS if (root / base).exists()))
            if args.manifest.stat().st_size > 4 * 1024 * 1024:
                raise ValueError("manifest exceeds 4 MiB")
            changes = _renames(root, sources, args.manifest.read_text(encoding="utf-8"))
            mode = args.rename
        else:
            if args.manifest or (args.all_sources and args.paths) or not (args.paths or args.all_sources):
                raise ValueError("select explicit paths or --all; --manifest is only for rename")
            paths = tuple(args.paths) if args.paths else tuple(base for base in ROOTS if (root / base).exists())
            sources = ReadSources(root, paths)
            if args.naming_audit:
                findings = tuple(finding for source in sources for finding in Audit(source))
                if args.json:
                    print(
                        json.dumps(
                            {
                                "findings": [
                                    {
                                        "path": f.path,
                                        "line": f.line,
                                        "column": f.column,
                                        "scope": f.scope,
                                        "name": f.name,
                                        "kind": f.kind,
                                        "rule": f.rule,
                                        "severity": f.severity,
                                        "message": f.message,
                                    }
                                    for f in findings
                                ],
                                "manifest_template": {
                                    "version": 1,
                                    "base_commit": _git_head(root),
                                    "files": [{"path": s.path, "sha256": s.digest} for s in sources],
                                    "renames": [],
                                    "references": [],
                                },
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                else:
                    for finding in findings:
                        print(
                            f"{finding.path}:{finding.line}:{finding.column}: {finding.severity} {finding.rule} {finding.scope}:{finding.name}: {finding.message}"
                        )
                    print(
                        f"errors={sum(f.severity == 'error' for f in findings)} review={sum(f.severity == 'review' for f in findings)}"
                    )
                return int(any(f.severity == "error" for f in findings))
            if args.json:
                raise ValueError("--json is only supported for naming audit")
            _require_versions("ruff")
            changes = tuple(
                change
                for source in sources
                if (change := FormatSource(source, PROJECT_ROOT / "pyproject.toml")).text != source.text
            )
            mode = args.format
        if mode == "preview":
            print(Diff(changes), end="")
        elif mode == "write":
            if args.rename and ReadSources(root, tuple(base for base in ROOTS if (root / base).exists())) != sources:
                raise ValueError("consumer inventory changed since rename analysis")
            WriteChanges(root, sources, changes)
        else:
            for change in changes:
                print(f"would change: {change.source.path}")
        print(f"files={len(sources)} changes={len(changes)} mode={mode}")
        return int(bool(changes) and mode != "write")
    except (OSError, ValueError, SyntaxError, PackageNotFoundError, subprocess.SubprocessError) as exc:
        print(f"quality tool: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("quality tool: cancelled before completion", file=sys.stderr)
        return 2
