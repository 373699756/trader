#!/usr/bin/env python3
"""Track whether the runtime dependency declaration matches the active Python."""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import re
import sys
from importlib import metadata
from pathlib import Path

_DEPENDENCY_FILES = ("pyproject.toml", "requirements/runtime.txt")
_MARKER_PATH = Path(".runtime/runtime-dependencies.sha256")
_PROJECT_NAME_PATTERN = re.compile(r"^name\s*=\s*['\"]([^'\"]+)['\"]\s*(?:#.*)?$")


def dependency_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    runtime_identity = (
        sys.implementation.name,
        str(sys.version_info.major),
        str(sys.version_info.minor),
        sys.platform,
        platform.machine(),
    )
    digest.update("\0".join(runtime_identity).encode("utf-8"))
    for relative_path in _DEPENDENCY_FILES:
        path = root / relative_path
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def marker_matches(root: Path) -> bool:
    marker = root / _MARKER_PATH
    try:
        return marker.read_text(encoding="utf-8").strip() == dependency_fingerprint(root)
    except (FileNotFoundError, OSError):
        return False


def project_distribution_name(root: Path) -> str:
    in_project_section = False
    for raw_line in (root / "pyproject.toml").read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            in_project_section = line == "[project]"
            continue
        if not in_project_section:
            continue
        match = _PROJECT_NAME_PATTERN.match(line)
        if match:
            return match.group(1).strip()
    raise ValueError("pyproject.toml does not define [project].name")


def project_distribution_installed(root: Path) -> bool:
    try:
        distribution_name = project_distribution_name(root)
        metadata.distribution(distribution_name)
    except (FileNotFoundError, OSError, ValueError, metadata.PackageNotFoundError):
        return False
    return True


def dependencies_ready(root: Path) -> bool:
    return marker_matches(root) and project_distribution_installed(root)


def write_marker(root: Path) -> None:
    marker = root / _MARKER_PATH
    marker.parent.mkdir(parents=True, exist_ok=True)
    temporary = marker.with_name(f".{marker.name}.{os.getpid()}.tmp")
    temporary.write_text(f"{dependency_fingerprint(root)}\n", encoding="utf-8")
    os.replace(temporary, marker)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("print", "check", "write"))
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args()
    root = args.root.resolve()

    if args.command == "print":
        print(dependency_fingerprint(root))
        return 0
    if args.command == "check":
        return 0 if dependencies_ready(root) else 1
    write_marker(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
