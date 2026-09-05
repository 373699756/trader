from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DESIGN = ROOT / "docs" / "software-business-design.md"
DECISION_EVENTS = ROOT / "src" / "trader" / "application" / "decisions" / "decision_events.py"


def test_authoritative_contract_freezes_unified_decision_identity_and_commit_event() -> None:
    design = DESIGN.read_text(encoding="utf-8")

    for token in (
        "ScoredDecision",
        "LongProjection",
        "UnifiedDecisionIndex",
        "expected_version",
        "DecisionCommitted",
        "按策略和交易日唯一",
    ):
        assert token in design


def test_committed_event_is_application_owned_and_research_free() -> None:
    tree = ast.parse(DECISION_EVENTS.read_text(encoding="utf-8"))
    classes = {node.name for node in tree.body if isinstance(node, ast.ClassDef)}
    imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module is not None}

    assert "DecisionCommitted" in classes
    assert all(not name.startswith(("trader.domain.research", "trader.application.research")) for name in imports)
