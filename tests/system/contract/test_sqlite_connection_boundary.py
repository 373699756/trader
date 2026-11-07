"""Only the shared SQLite primitive may construct a driver connection."""

import ast
from pathlib import Path

from tests.helpers.shared.python_source import source_tree

_DRIVER_OBSERVERS = {
    "scripts/training/jobs/training_source_guard.py": frozenset({"ReadOnlyTrainingSource"}),
    "tests/download/unit/history/test_history_download_index.py": frozenset({"test_selected_population_loads_with_999_sqlite_variable_limit"}),
    "tests/training/unit/scripts/test_training_alignment.py": frozenset({"test_published_training_source_rejects_writes_and_restores_all_hooks"}),
}


def _raw_connection_lines(tree: ast.AST, *, observers: frozenset[str] = frozenset()) -> tuple[int, ...]:
    modules = {
        alias.asname or alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name in {"sqlite3", "sqlite3.dbapi2"}
    }
    # These exact scopes intercept or compare the existing driver factory. Even
    # there, calling its constructor directly remains a boundary violation.
    observed = {
        id(node)
        for function in ast.walk(tree)
        if isinstance(function, ast.FunctionDef) and function.name in observers
        for node in ast.walk(function)
        if isinstance(node, ast.Attribute)
    }
    called = {id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in {"sqlite3", "sqlite3.dbapi2"}:
            if any(alias.name in {"connect", "Connection", "*"} for alias in node.names):
                violations.append(node.lineno)
            modules.update(alias.asname or alias.name for alias in node.names if alias.name == "dbapi2")
        if isinstance(node, ast.Attribute) and node.attr == "connect":
            if id(node) in observed and id(node) not in called:
                continue
            owner = node.value
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "Connection":
            owner = node.func.value
        else:
            continue
        while isinstance(owner, ast.Attribute):
            owner = owner.value
        if isinstance(owner, ast.Name) and owner.id in modules:
            violations.append(node.lineno)
    return tuple(sorted(violations))


def test_all_python_consumers_use_the_shared_sqlite_connection_boundary():
    root = Path(__file__).resolve().parents[3]
    owner = root / "src/trader/infra/sqlite_connection.py"
    failures = []
    paths = list(root.glob("*.py"))
    for directory in ("src", "scripts", "tests"):
        paths.extend((root / directory).rglob("*.py"))
    for path in paths:
        if path == owner:
            continue
        tree = source_tree(path)
        relative = path.relative_to(root).as_posix()
        failures.extend(f"{relative}:{line}" for line in _raw_connection_lines(
            tree, observers=_DRIVER_OBSERVERS.get(relative, frozenset()),
        ))
        # Include embedded Python executed by subprocesses in crash tests.
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and "import sqlite3" in node.value:
                try:
                    lines = _raw_connection_lines(ast.parse(node.value))
                except SyntaxError:
                    continue
                if lines:
                    failures.append(f"{path.relative_to(root)}:{node.lineno}:embedded")
    assert failures == []


def test_driver_observers_cannot_construct_connections_or_exempt_other_scopes():
    tree = ast.parse("\n".join((
        "import sqlite3",
        "def observe():",
        "    original = sqlite3.connect",
        "    sqlite3.connect(':memory:')",
        "def other():",
        "    original = sqlite3.connect",
    )))
    assert _raw_connection_lines(tree, observers=frozenset({"observe"})) == (4, 6)


def test_business_code_uses_explicit_transactions_and_has_no_driver_import_in_application():
    root = Path(__file__).resolve().parents[3] / "src/trader"
    failures = []
    for path in root.rglob("*.py"):
        if path == root / "infra/sqlite_connection.py":
            continue
        for node in ast.walk(source_tree(path)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "apply_script":
                    failures.append(f"{path.relative_to(root)}:{node.lineno}:script")
                if node.func.attr == "run" and node.args and isinstance(node.args[0], ast.Constant):
                    statement = node.args[0].value
                    if isinstance(statement, str) and statement.lstrip().upper().startswith(
                        ("BEGIN", "COMMIT", "ROLLBACK")
                    ):
                        failures.append(f"{path.relative_to(root)}:{node.lineno}:transaction")
            if "application" in path.parts or "domain" in path.parts:
                imports = (
                    [item.name for item in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                    if isinstance(node, ast.ImportFrom)
                    else []
                )
                if any(name == "sqlite3" or name == "trader.infra.sqlite_connection" for name in imports):
                    failures.append(f"{path.relative_to(root)}:{node.lineno}:dependency")
    assert failures == []
