"""Single machine rule set for opt-in naming audits and rename admission."""

from __future__ import annotations

import ast
import builtins
import keyword
import re
from dataclasses import dataclass

from .files import SourceFile

BANNED_WORDS = frozenset({"store", "stored", "repository", "archive", "registry", "active"})
VAGUE_NAMES = frozenset({"data", "obj", "tmp", "manager"})
BUILTIN_NAMES = frozenset(dir(builtins))


@dataclass(frozen=True)
class NamingFinding:
    path: str
    line: int
    column: int
    scope: str
    name: str
    rule: str
    severity: str
    message: str
    kind: str = "module"


@dataclass(frozen=True)
class Symbol:
    name: str
    scope: str
    kind: str
    line: int
    column: int


def Words(name: str) -> tuple[str, ...]:
    return tuple(word.lower() for word in re.findall(r"[A-Z]+(?=[A-Z][a-z]|\d|_|$)|[A-Z]?[a-z]+|[A-Z]+|\d+", name))


def NameIssues(name: str, kind: str) -> tuple[tuple[str, str, str], ...]:
    issues: list[tuple[str, str, str]] = []
    if not name.isidentifier() or keyword.iskeyword(name):
        return (("NAME001", "error", "not a valid non-keyword identifier"),)
    words = set(Words(name))
    forbidden = words & BANNED_WORDS
    if forbidden:
        issues.append(("NAME002", "error", f"forbidden responsibility word: {', '.join(sorted(forbidden))}"))
    if name in BUILTIN_NAMES and not (name.startswith("__") and name.endswith("__")):
        issues.append(("NAME003", "error", "shadows a Python builtin; fixed external contracts require review"))
    if name in {"l", "I", "O"}:
        issues.append(("NAME004", "error", "ambiguous single-letter name"))
    elif len(name) == 1 and name != "_":
        issues.append(("NAME005", "review", "confirm loop-index or mathematical meaning"))
    if name.strip("_").lower() in VAGUE_NAMES or words & {"manager", "artifacts"}:
        issues.append(("NAME006", "review", "describe the actual responsibility; no automatic replacement"))
    if name.startswith("__") and name.endswith("__"):
        return tuple(issues)
    if name.startswith("__"):
        issues.append(
            ("NAME007", "review", "double prefix changes name mangling; do not introduce for ordinary privacy")
        )
    valid = True
    if kind == "constant":
        valid = re.fullmatch(r"_?[A-Z][A-Z0-9_]*", name) is not None
    elif kind in {"class", "type"} or (kind == "function" and not name.startswith("_")):
        valid = re.fullmatch(r"_?[A-Z][A-Za-z0-9]*", name) is not None
    elif kind in {"function", "method", "attribute", "parameter"}:
        valid = re.fullmatch(r"_?[a-z][a-z0-9_]*|_", name) is not None
    elif kind != "module":
        valid = re.fullmatch(r"_?[a-z][a-z0-9_]*|_?[A-Z][A-Z0-9_]*|_", name) is not None
    else:
        valid = re.fullmatch(r"_?[a-z][a-z0-9_]*|__init__", name) is not None
    if not valid:
        issues.append(("NAME008", "error", f"invalid naming shape for {kind}"))
    if kind in {"parameter", "variable"} and name.startswith("_"):
        issues.append(("NAME009", "review", "underscore only for discarded values or required unused parameters"))
    return tuple(issues)


def Symbols(text: str) -> tuple[Symbol, ...]:
    result: list[Symbol] = []
    seen: set[tuple[str, str, str]] = set()

    def add(name: str, scope: str, kind: str, node: ast.AST) -> None:
        if not isinstance(node, (ast.stmt, ast.expr, ast.arg, ast.alias, ast.ExceptHandler, ast.pattern)):
            return
        key = (scope, name, kind)
        if key not in seen or kind in {"class", "function", "method"}:
            seen.add(key)
            result.append(Symbol(name, scope, kind, node.lineno, node.col_offset))

    def visit(node: ast.AST, scope: str, parent_kind: str) -> None:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            kind = "class" if isinstance(node, ast.ClassDef) else "method" if parent_kind == "class" else "function"
            add(node.name, scope, kind, node)
            scope = f"{scope}.{node.name}" if scope else node.name
            parent_kind = kind
        elif isinstance(node, ast.arg):
            add(node.arg, scope, "parameter", node)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and (
                isinstance(node.annotation, ast.Name)
                and node.annotation.id == "TypeAlias"
                or isinstance(node.annotation, ast.Attribute)
                and node.annotation.attr == "TypeAlias"
            )
        ):
            add(node.target.id, scope, "type", node.target)
            if node.value is not None:
                visit(node.value, scope, parent_kind)
            return
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            kind = (
                "constant"
                if node.id.isupper()
                else "global"
                if not scope
                else "attribute"
                if parent_kind == "class"
                else "variable"
            )
            add(node.id, scope, kind, node)
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
            if isinstance(node.value, ast.Name) and node.value.id in {"self", "cls"}:
                add(node.attr, scope.rpartition(".")[0], "attribute", node)
        elif isinstance(node, ast.alias) and node.asname:
            add(node.asname, scope, "variable", node)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            add(node.name, scope, "variable", node)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            add(node.name, scope, "variable", node)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            add(node.rest, scope, "variable", node)
        for child in ast.iter_child_nodes(node):
            visit(child, scope, parent_kind)

    visit(ast.parse(text), "", "module")
    return tuple(result)


def Audit(source: SourceFile) -> tuple[NamingFinding, ...]:
    findings: list[NamingFinding] = []
    symbols = list(Symbols(source.text))
    for part in source.path.removesuffix(".py").split("/"):
        symbols.append(Symbol(part, "<path>", "module", 1, 0))
        if re.fullmatch(r"v\d+", part, re.IGNORECASE) and part.lower() not in {"v1", "v2", "v3", "v5"}:
            findings.append(
                NamingFinding(
                    source.path, 1, 0, "<path>", part, "NAME011", "error", "unapproved product profile directory"
                )
            )
    for symbol in symbols:
        for rule, severity, message in NameIssues(symbol.name, symbol.kind):
            # Pytest owns discovery names; third-party overrides remain explicit review findings.
            if rule == "NAME008" and source.path.startswith("tests/") and symbol.name.startswith("test_"):
                continue
            findings.append(
                NamingFinding(
                    source.path,
                    symbol.line,
                    symbol.column,
                    symbol.scope,
                    symbol.name,
                    rule,
                    severity,
                    message,
                    symbol.kind,
                )
            )
    for node in ast.walk(ast.parse(source.text)):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id == "__all__" for target in targets):
                if isinstance(node.value, (ast.List, ast.Tuple)):
                    for item in node.value.elts:
                        if (
                            isinstance(item, ast.Constant)
                            and isinstance(item.value, str)
                            and item.value.startswith("_")
                        ):
                            findings.append(
                                NamingFinding(
                                    source.path,
                                    item.lineno,
                                    item.col_offset,
                                    "",
                                    item.value,
                                    "NAME010",
                                    "error",
                                    "private symbol exported in __all__",
                                )
                            )
    return tuple(findings)
