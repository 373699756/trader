"""Single implementation of immutable artifact publication.

Research artifacts are append-only files: the first writer creates a path and every
later writer must prove the existing file carries the same identity instead of
overwriting it. These primitives own the temporary-file, flush and hard-link mechanics,
so each artifact family keeps only its own notion of "the same identity".
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path


def publish_immutable(path: Path, rendered: str) -> bool:
    """Create ``path`` with ``rendered`` unless it already exists.

    Returns True when this call created the file. A concurrent writer that wins the
    race keeps its content and this call returns False, so the caller verifies the
    persisted identity instead of replacing it.
    """

    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        return True
    finally:
        temporary.unlink(missing_ok=True)


def publish_immutable_file(path: Path, source: Path) -> bool:
    """Copy ``source`` to ``path`` unless ``path`` already exists.

    Mirrors :func:`publish_immutable` for artifacts published as a whole file rather
    than as canonical JSON text.
    """

    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copyfile(source, temporary)
        _flush_file(temporary)
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        return True
    finally:
        temporary.unlink(missing_ok=True)


def replace_file(path: Path, rendered: str) -> None:
    """Replace ``path`` with ``rendered`` so readers never observe a partial file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        _discard(temporary_name)
        raise


def _flush_file(path: Path) -> None:
    # A writable handle keeps this valid on platforms that reject fsync of read-only handles.
    with path.open("rb+") as handle:
        os.fsync(handle.fileno())


def _discard(temporary_name: str) -> None:
    try:
        os.unlink(temporary_name)
    except FileNotFoundError:
        pass


__all__ = [
    "publish_immutable",
    "publish_immutable_file",
    "replace_file",
]
