"""Only the shared SQLite primitive may construct a driver connection."""

import ast
from pathlib import Path


def _raw_connection_lines(source: str) -> tuple[int, ...]:
    tree = ast.parse(source)
    modules = {
        alias.asname or alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name in {"sqlite3", "sqlite3.dbapi2"}
    }
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in {"sqlite3", "sqlite3.dbapi2"}:
            if any(alias.name in {"connect", "Connection", "*"} for alias in node.names):
                violations.append(node.lineno)
            modules.update(alias.asname or alias.name for alias in node.names if alias.name == "dbapi2")
        if isinstance(node, ast.Attribute) and node.attr == "connect":
            owner = node.value
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "Connection":
            owner = node.func.value
        else:
            continue
        while isinstance(owner, ast.Attribute):
            owner = owner.value
        if isinstance(owner, ast.Name) and owner.id in modules:
            violations.append(node.lineno)
    return tuple(violations)


def test_all_python_consumers_use_the_shared_sqlite_connection_boundary():
    root = Path(__file__).resolve().parents[2]
    owner = root / "src/trader/infra/sqlite_connection.py"
    failures = []
    paths = list(root.glob("*.py"))
    for directory in ("src", "scripts", "tests"):
        paths.extend((root / directory).rglob("*.py"))
    for path in paths:
        if path == owner:
            continue
        source = path.read_text(encoding="utf-8")
        failures.extend(f"{path.relative_to(root)}:{line}" for line in _raw_connection_lines(source))
        # Include embedded Python executed by subprocesses in crash tests.
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and "import sqlite3" in node.value:
                try:
                    lines = _raw_connection_lines(node.value)
                except SyntaxError:
                    continue
                if lines:
                    failures.append(f"{path.relative_to(root)}:{node.lineno}:embedded")
    assert failures == []
