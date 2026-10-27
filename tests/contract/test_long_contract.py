from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = ROOT / "src" / "trader" / "bootstrap.py"
LONG_RUNTIME = ROOT / "src" / "trader" / "recommendation" / "application" / "long_runtime.py"


def test_production_composition_installs_long_without_legacy_snapshot_publication() -> None:
    source = BOOTSTRAP.read_text(encoding="utf-8")
    runtime_source = LONG_RUNTIME.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}

    assert "trader.recommendation.application.long_runtime" in imports
    assert "long_runtime=publication.long_runtime" in source
    assert "DecisionBuildDependencies(" in source
    assert "publication.long_runtime," in source
    for forbidden in (
        "RecommendationSnapshot",
        "DeepSeek",
        "FreezeCoordinator",
        "DecisionRecordPort",
        "settle_outcomes",
    ):
        assert forbidden not in runtime_source
