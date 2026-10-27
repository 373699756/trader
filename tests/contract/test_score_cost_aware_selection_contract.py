from __future__ import annotations

import ast
from pathlib import Path

from trader.training.domain.evaluation.cost_aware_selection import COST_AWARE_UTILITY_FIELDS

ROOT = Path(__file__).resolve().parents[2]


def test_cost_aware_selection_keeps_utility_fields_and_research_isolation() -> None:
    source_paths = (
        ROOT / "src" / "trader" / "training" / "domain" / "evaluation" / "cost_aware_selection.py",
        ROOT / "src" / "trader" / "training" / "application" / "cost_aware_selection.py",
        ROOT / "src" / "trader" / "training" / "application" / "cost_aware_selection_report.py",
    )

    assert COST_AWARE_UTILITY_FIELDS == ("gross_expected_excess", "estimated_cost")
    assert "score_tomorrow_cost_aware_selection_report" in source_paths[2].read_text(encoding="utf-8")
    for path in source_paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names} | {
            node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        }
        assert not any(name.startswith(("trader.bootstrap", "trader.web", "trader.infra")) for name in imports)
        source = path.read_text(encoding="utf-8")
        assert "residual_momentum" not in source
        assert "stability_score" not in source
