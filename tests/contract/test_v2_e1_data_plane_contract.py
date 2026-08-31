from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DESIGN = ROOT / "docs" / "software-business-design.md"
STRATEGY = ROOT / "docs" / "recommendation-strategy.md"
MARKET_PORT = ROOT / "src" / "trader" / "application" / "ports" / "market.py"


def test_authoritative_contract_freezes_data_plane_port_lineage_and_coverage() -> None:
    design = DESIGN.read_text(encoding="utf-8")
    strategy = STRATEGY.read_text(encoding="utf-8")

    for token in (
        "DataPlaneReadPort",
        "valid/degraded/stale/missing/conflicting",
        "潜在可执行代码必须 100%",
        "候选历史覆盖率是加法健康指标",
        "游标只能作为增量位置，不能代替实际交易日历内容",
    ):
        assert token in design
    assert "只通过应用层 `DataPlaneReadPort` 读取" in strategy
    assert "证券主数据覆盖为 100%" in strategy


def test_data_plane_read_port_is_application_owned_and_infrastructure_free() -> None:
    tree = ast.parse(MARKET_PORT.read_text(encoding="utf-8"))
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
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(isinstance(node, ast.ClassDef) and node.name == "DataPlaneReadPort" for node in tree.body):
            definitions.append(path)

    assert definitions == [MARKET_PORT]
