"""Read-only source inventory of the shared SQLite policy injection boundary."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TypedDict


class _ConnectionCall(TypedDict):
    path: str
    line: int
    call: str


def AuditSqlitePolicyConsumers(project: Path) -> dict[str, object]:
    """Report explicit and unpacked injection separately; never infer runtime coverage."""
    source = project / "src/trader"
    primitive = source / "infra/sqlite_connection.py"
    consumers: list[_ConnectionCall] = []
    missing: list[_ConnectionCall] = []
    indirect: list[_ConnectionCall] = []
    for path in sorted(source.rglob("*.py")):
        if path == primitive:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        functions = {"SqliteSession", "OpenSqliteConnection"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "trader.infra.sqlite_connection":
                functions.update(alias.asname or alias.name for alias in node.names if alias.name in functions)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else ""
            if name not in functions:
                continue
            location: _ConnectionCall = {"path": path.relative_to(project).as_posix(), "line": node.lineno, "call": name}
            consumers.append(location)
            if not any(keyword.arg == "sqlite_policy" for keyword in node.keywords):
                (indirect if any(keyword.arg is None for keyword in node.keywords) else missing).append(location)
    return {
        "read_only": True,
        "source_files": len(tuple(source.rglob("*.py"))),
        "connection_calls": len(consumers),
        "explicit_policy_calls": len(consumers) - len(missing) - len(indirect),
        "missing_policy_calls": missing,
        "unpacked_policy_calls_require_review": indirect,
        "consumers": consumers,
        "scope": "source injection inventory; sentinel-policy adapter tests establish runtime behavior separately",
    }
