from __future__ import annotations

import ast
from pathlib import Path

from trader.domain.research.cost_aware_selection import COST_AWARE_UTILITY_FIELDS

ROOT = Path(__file__).resolve().parents[2]


def test_cost_aware_selection_is_documented_and_exploratory_only() -> None:
    strategy = (ROOT / "docs" / "recommendation-strategy.md").read_text(encoding="utf-8")
    work = (ROOT / "docs" / "work.md").read_text(encoding="utf-8")
    source_paths = (
        ROOT / "src" / "trader" / "domain" / "research" / "cost_aware_selection.py",
        ROOT / "src" / "trader" / "application" / "research" / "cost_aware_selection.py",
        ROOT / "src" / "trader" / "application" / "research" / "cost_aware_selection_models.py",
    )

    assert COST_AWARE_UTILITY_FIELDS == ("gross_expected_excess", "estimated_cost")
    assert "score_tomorrow_cost_aware_selection" in strategy
    assert "score_tomorrow_cost_aware_selection_report" in work
    for path in source_paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names} | {
            node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        }
        assert not any(name.startswith(("trader.bootstrap", "trader.web", "trader.infra")) for name in imports)
        source = path.read_text(encoding="utf-8")
        assert "residual_momentum" not in source
        assert "stability_score" not in source
