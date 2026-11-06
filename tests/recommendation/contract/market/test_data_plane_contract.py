from __future__ import annotations

import ast
from pathlib import Path

from tests.helpers.shared.python_source import source_tree

ROOT = Path(__file__).resolve().parents[4]
MARKET_PORT = ROOT / "src" / "trader" / "recommendation" / "application" / "ports" / "market_data.py"


def test_data_plane_read_port_is_application_owned_and_infrastructure_free() -> None:
    tree = source_tree(MARKET_PORT)
    classes = {node.name for node in tree.body if isinstance(node, ast.ClassDef)}
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)

    assert "DataPlaneReadPort" in classes
    assert all(not name.startswith(("trader.infra", "flask", "stock_analyzer")) for name in imports)


def test_data_plane_read_port_has_one_canonical_definition() -> None:
    definitions: list[Path] = []
    for path in (ROOT / "src" / "trader").rglob("*.py"):
        tree = source_tree(path)
        if any(isinstance(node, ast.ClassDef) and node.name == "DataPlaneReadPort" for node in tree.body):
            definitions.append(path)

    assert definitions == [MARKET_PORT]
