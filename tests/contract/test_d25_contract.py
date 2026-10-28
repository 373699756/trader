from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = ROOT / "src" / "trader" / "bootstrap.py"


def test_production_composition_installs_d25_and_unified_query() -> None:
    source = BOOTSTRAP.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}

    assert "trader.recommendation.application.pipeline.freeze_publish" in imports
    assert "freeze_coordinator.ScoredFreezeCoordinator" in source
    assert "freeze_queries=publication.freeze_queries" in source
    assert "freeze_events=publication.freeze_events" in source
    assert "strategy=Strategy.D25" in source
