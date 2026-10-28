"""Strict JSON boundary for reviewable, content-bound rename batches."""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class FileIdentity:
    path: str
    sha256: str


@dataclass(frozen=True)
class RenameItem:
    path: str
    scope: str
    kind: str
    old: str
    new: str
    line: int
    column: int
    visibility: str
    reason: str


@dataclass(frozen=True)
class StringReference:
    path: str
    line: int
    old: str
    new: str
    reason: str


@dataclass(frozen=True)
class Manifest:
    base_commit: str
    files: tuple[FileIdentity, ...]
    renames: tuple[RenameItem, ...]
    references: tuple[StringReference, ...]


def _object(value: object, fields: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != fields or not all(isinstance(key, str) for key in value):
        raise ValueError(f"manifest object must contain exactly {sorted(fields)}")
    return value


def _text(value: object, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ValueError("manifest requires a nonempty string")
    return value


def _integer(value: object, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError("manifest requires a nonnegative integer position")
    return value


def _array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError("manifest requires an array")
    return value


def ParseManifest(text: str) -> Manifest:
    raw = _object(json.loads(text), {"version", "base_commit", "files", "renames", "references"})
    if type(raw["version"]) is not int or raw["version"] != 1:
        raise ValueError("unsupported manifest version")
    files: list[FileIdentity] = []
    for value in _array(raw["files"]):
        item = _object(value, {"path", "sha256"})
        files.append(FileIdentity(_text(item["path"]), _text(item["sha256"])))
    renames: list[RenameItem] = []
    for value in _array(raw["renames"]):
        item = _object(value, {"path", "scope", "kind", "old", "new", "line", "column", "visibility", "reason"})
        renames.append(
            RenameItem(
                _text(item["path"]),
                _text(item["scope"], empty=True),
                _text(item["kind"]),
                _text(item["old"]),
                _text(item["new"]),
                _integer(item["line"], 1),
                _integer(item["column"], 0),
                _text(item["visibility"]),
                _text(item["reason"]),
            )
        )
    references: list[StringReference] = []
    for value in _array(raw["references"]):
        item = _object(value, {"path", "line", "old", "new", "reason"})
        references.append(
            StringReference(
                _text(item["path"]),
                _integer(item["line"], 1),
                _text(item["old"]),
                _text(item["new"]),
                _text(item["reason"]),
            )
        )
    if not files or not renames or len({item.path for item in files}) != len(files):
        raise ValueError("manifest needs unique files and at least one rename")
    return Manifest(_text(raw["base_commit"]), tuple(files), tuple(renames), tuple(references))
