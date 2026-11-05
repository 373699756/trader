"""Bounded source selection and optimistic, per-file replacement."""

from __future__ import annotations

import difflib
import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOTS = ("src/trader", "scripts", "tests")
EXCLUDED = frozenset({"__pycache__", ".pytest_cache", ".ruff_cache", "vendor"})
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024


@dataclass(frozen=True)
class SourceFile:
    path: str
    content: bytes

    @property
    def text(self) -> str:
        return self.content.decode("utf-8")

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


@dataclass(frozen=True)
class Change:
    source: SourceFile
    text: str


def SafePath(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"unsafe relative path: {relative}")
    if not any(path == Path(base) or Path(base) in path.parents for base in ROOTS):
        raise ValueError(f"path outside source roots: {relative}")
    cursor = root
    for part in path.parts:
        cursor /= part
        if cursor.is_symlink() or part in EXCLUDED:
            raise ValueError(f"excluded path: {relative}")
    return root / path


def ReadSources(root: Path, paths: tuple[str, ...]) -> tuple[SourceFile, ...]:
    selected: set[Path] = set()
    for relative in paths:
        path = SafePath(root, relative)
        if not path.exists():
            raise ValueError(f"missing path: {relative}")
        if path.is_dir():
            for directory, subdirs, names in os.walk(path, followlinks=False):
                subdirs[:] = sorted(name for name in subdirs if name not in EXCLUDED)
                if any((Path(directory) / name).is_symlink() for name in (*subdirs, *names)):
                    raise ValueError(f"symlink in source tree: {relative}")
                selected.update(Path(directory) / name for name in names if name.endswith(".py"))
        elif path.suffix == ".py":
            selected.add(path)
        else:
            raise ValueError(f"expected Python source: {relative}")
    result: list[SourceFile] = []
    total = 0
    for path in sorted(selected):
        relative = path.relative_to(root).as_posix()
        SafePath(root, relative)
        with path.open("rb") as stream:
            content = stream.read(MAX_FILE_BYTES + 1)
        total += len(content)
        if len(content) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
            raise ValueError("source size limit exceeded (4 MiB/file, 128 MiB/job)")
        source = SourceFile(relative, content)
        content.decode("utf-8")
        result.append(source)
    return tuple(result)


def Diff(changes: tuple[Change, ...]) -> str:
    return "".join(
        line
        for change in changes
        for line in difflib.unified_diff(
            change.source.text.splitlines(keepends=True),
            change.text.splitlines(keepends=True),
            fromfile=change.source.path,
            tofile=change.source.path,
        )
    )


def WriteChanges(root: Path, sources: tuple[SourceFile, ...], changes: tuple[Change, ...]) -> None:
    for source in sources:
        if SafePath(root, source.path).read_bytes() != source.content:
            raise ValueError(f"source changed since analysis: {source.path}")
    completed: list[str] = []
    for change in changes:
        temporary: Path | None = None
        try:
            path = SafePath(root, change.source.path)
            if path.read_bytes() != change.source.content:
                raise ValueError(f"source changed before replacement: {change.source.path}")
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".quality-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(change.text.encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(path.stat().st_mode & 0o777)
            if path.read_bytes() != change.source.content:
                raise ValueError(f"source changed during replacement: {change.source.path}")
            os.replace(temporary, path)
            completed.append(change.source.path)
        except (OSError, ValueError, KeyboardInterrupt) as exc:
            raise ValueError(f"replacement stopped; completed={completed}; failed={change.source.path}: {exc}") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
