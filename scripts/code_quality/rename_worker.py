"""Rope runs only in a disposable source copy under a parent-enforced timeout."""

from __future__ import annotations

import ast
import io
import json
import re
import sys
import tempfile
import tokenize
from pathlib import Path

from rope.base.exceptions import RopeError
from rope.base.project import Project
from rope.refactor.rename import Rename

from .manifest import Manifest, ParseManifest, RenameItem
from .naming import NameIssues, Symbols


def _offset(text: str, item: RenameItem, scope: str) -> int:
    candidates = [s for s in Symbols(text) if s.name == item.old and s.scope == scope and s.kind == item.kind]
    if len(candidates) != 1:
        raise ValueError(f"ambiguous definition: {item.path}:{scope}:{item.old}")
    symbol = candidates[0]
    lines = text.splitlines(keepends=True)
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type == tokenize.NAME and token.string == item.old and token.start[0] == symbol.line:
            if token.start[1] >= len(lines[symbol.line - 1].encode()[: symbol.column].decode()):
                return sum(len(line) for line in lines[: token.start[0] - 1]) + token.start[1]
    raise ValueError("cannot resolve definition token")


def _string_edits(root: Path, manifest: Manifest) -> None:
    used: set[int] = set()
    for identity in manifest.files:
        path = root / identity.path
        text = path.read_text(encoding="utf-8")
        edits: list[tuple[int, int, str]] = []
        lines = text.splitlines(keepends=True)
        tree = ast.parse(text)
        exports: set[int] = set()
        for statement in tree.body:
            if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
                continue
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            if any(isinstance(target, ast.Name) and target.id == "__all__" for target in targets):
                if not isinstance(statement.value, (ast.List, ast.Tuple)) or not all(
                    isinstance(element, ast.Constant) and isinstance(element.value, str)
                    for element in statement.value.elts
                ):
                    raise ValueError(f"dynamic export contract requires separate migration: {identity.path}")
                exports.update(id(element) for element in statement.value.elts)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            hits = [item for item in manifest.renames if re.search(r"\b" + re.escape(item.old) + r"\b", node.value)]
            if not hits:
                continue
            references = [
                (index, r)
                for index, r in enumerate(manifest.references)
                if r.path == identity.path and r.line == node.lineno and r.old == node.value
            ]
            own_export = [
                item for item in hits if item.path == identity.path and not item.scope and node.value == item.old
            ]
            if id(node) in exports and len(own_export) == 1 and not references:
                replacement = own_export[0].new
            elif len(references) == 1:
                index, reference = references[0]
                if index in used:
                    raise ValueError("string reference matches multiple literals")
                used.add(index)
                replacement = reference.new
            else:
                raise ValueError(f"unresolved string reference: {identity.path}:{node.lineno}")
            assert node.end_lineno is not None and node.end_col_offset is not None
            start = sum(map(len, lines[: node.lineno - 1])) + len(
                lines[node.lineno - 1].encode()[: node.col_offset].decode()
            )
            end = sum(map(len, lines[: node.end_lineno - 1])) + len(
                lines[node.end_lineno - 1].encode()[: node.end_col_offset].decode()
            )
            edits.append((start, end, repr(replacement)))
        for start, end, replacement in sorted(edits, reverse=True):
            text = text[:start] + replacement + text[end:]
        path.write_text(text, encoding="utf-8", newline="")
    if len(used) != len(manifest.references):
        raise ValueError("unused or stale explicit string reference")


def _tokens(text: str, old: str, new: str) -> tuple[tuple[int, str], ...]:
    return tuple(
        (token.type, old if token.type == tokenize.NAME and token.string == new else token.string)
        for token in tokenize.generate_tokens(io.StringIO(text).readline)
    )


def Execute(root: Path, manifest: Manifest) -> None:
    project = Project(
        str(root), ropefolder=None, source_folders=["src", "scripts", "tests"], save_objectdb=False, save_history=False
    )
    try:
        identities: set[tuple[str, str, str]] = set()
        for item in manifest.renames:
            key = (item.path, item.scope, item.old)
            if key in identities or item.new == item.old:
                raise ValueError("duplicate or unchanged rename")
            identities.add(key)
            if item.visibility not in {"public", "internal"} or (
                item.new.startswith("_") != (item.visibility == "internal")
            ):
                raise ValueError("new name disagrees with explicit visibility")
            if any(severity == "error" for _, severity, _ in NameIssues(item.new, item.kind)):
                raise ValueError(f"new name violates naming rules: {item.new}")
            if item.old.startswith("__") or item.new.startswith("__"):
                raise ValueError("protocol/name-mangled symbols cannot be batch renamed")
            source = (root / item.path).read_text(encoding="utf-8")
            matches = [s for s in Symbols(source) if (s.scope, s.name, s.kind) == (item.scope, item.old, item.kind)]
            if len(matches) != 1 or (matches[0].line, matches[0].column) != (item.line, item.column):
                raise ValueError("definition position does not match manifest")
            if item.kind == "method":
                for node in ast.walk(ast.parse(source)):
                    if isinstance(node, ast.ClassDef) and node.name == item.scope.split(".")[-1] and node.bases:
                        raise ValueError("inherited/external method contracts require a separate reviewed migration")
            # Conservative collision policy also prevents swaps and accidental shadowing.
            if any(
                s.name == item.new
                for identity in manifest.files
                for s in Symbols((root / identity.path).read_text(encoding="utf-8"))
            ):
                raise ValueError(f"target name already defined: {item.new}")
        if len({item.new for item in manifest.renames}) != len(manifest.renames):
            raise ValueError("duplicate target name in batch")
        # Pin string positions before Rope changes identifier widths. Exact entries can retain external strings.
        _string_edits(root, manifest)
        completed: list[RenameItem] = []
        for item in manifest.renames:
            source = (root / item.path).read_text(encoding="utf-8")
            scope = item.scope
            for previous in completed:
                if previous.path == item.path:
                    prefix = ".".join(part for part in (previous.scope, previous.old) if part)
                    if scope == prefix or scope.startswith(prefix + "."):
                        scope = ".".join(part for part in (previous.scope, previous.new) if part) + scope[len(prefix) :]
            resource = project.get_file(item.path)
            project.validate()
            rename = Rename(project, resource, _offset(source, item, scope))
            uncertain: list[int] = []

            def unsure(occurrence: object, found: list[int] = uncertain) -> bool:
                found.append(1)
                return False

            changes = rename.get_changes(item.new, docs=False, unsure=unsure)
            if uncertain:
                raise ValueError(f"unresolved dynamic references for {item.old}")
            before = {entry.path: (root / entry.path).read_text(encoding="utf-8") for entry in manifest.files}
            project.do(changes)
            for entry in manifest.files:
                after = (root / entry.path).read_text(encoding="utf-8")
                if _tokens(before[entry.path], item.old, item.new) != _tokens(after, item.old, item.new):
                    raise ValueError("Rope changed tokens beyond the approved identifier")
            completed.append(item)
        for identity in manifest.files:
            ast.parse((root / identity.path).read_text(encoding="utf-8"))
    finally:
        project.close()


def Main() -> int:
    try:
        root = Path(sys.argv[1])
        if root.parent.resolve() != Path(tempfile.gettempdir()).resolve() or not root.name.startswith("trader-rename-"):
            raise ValueError("worker accepts only a parent-created external temporary project")
        manifest = ParseManifest(Path(sys.argv[2]).read_text(encoding="utf-8"))
        Execute(root, manifest)
        print(
            json.dumps(
                {identity.path: (root / identity.path).read_text(encoding="utf-8") for identity in manifest.files}
            )
        )
        return 0
    except (OSError, ValueError, SyntaxError, RopeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(Main())
