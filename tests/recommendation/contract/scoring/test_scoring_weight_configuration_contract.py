from __future__ import annotations

import ast
from pathlib import Path

from tests.helpers.shared.python_source import source_text, source_tree

ROOT = Path(__file__).resolve().parents[4]


def _read(relative: str) -> str:
    return source_text(ROOT / relative)


def _function(relative: str, name: str) -> ast.FunctionDef:
    tree = source_tree(ROOT / relative)
    return next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == name)


def _is_weight_literal(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return 0.0 < float(node.value) < 1.0
    return (
        isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Div)
        and isinstance(node.left, ast.Constant)
        and isinstance(node.right, ast.Constant)
        and isinstance(node.left.value, (int, float))
        and isinstance(node.right.value, (int, float))
        and 0.0 < float(node.left.value) / float(node.right.value) < 1.0
    )


def test_production_weight_numbers_have_no_validator_copy() -> None:
    validation = _read("src/trader/recommendation/infra/settings/strategy_validation.py")

    assert "_FIXED_DIMENSION_WEIGHTS" not in validation
    assert "_FIXED_BOARD_CANDIDATE_WEIGHTS" not in validation
    assert "_FIXED_BOARD_LOCAL_WEIGHTS" not in validation
    assert "_validate_fixed_vector" not in validation
    assert "fixed at 0.68" not in validation


def test_domain_weight_consumers_do_not_multiply_by_numeric_literals() -> None:
    scoring_path = "src/trader/recommendation/domain/scoring/scoring.py"
    for function_name in ("ScoreBoardStrategy",):
        function = _function(scoring_path, function_name)
        numeric_multipliers = [
            node
            for node in ast.walk(function)
            if isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Mult)
            and (_is_weight_literal(node.left) or _is_weight_literal(node.right))
        ]
        assert numeric_multipliers == []

    fusion = _read("src/trader/recommendation/domain/risk/fusion.py")
    assert "FusionPolicy(local_weight: float = 0.68" not in fusion
    assert "fusion weights are fixed at 0.68/0.32" not in fusion

    for relative, function_names in (
        ("src/trader/recommendation/infra/normalization/features.py", ("_raw_features",)),
        ("src/trader/recommendation/domain/market/research.py", ("_industry_policy_score", "_protection_score")),
    ):
        for function_name in function_names:
            function = _function(relative, function_name)
            numeric_multipliers = [
                node
                for node in ast.walk(function)
                if isinstance(node, ast.BinOp)
                and isinstance(node.op, ast.Mult)
                and (_is_weight_literal(node.left) or _is_weight_literal(node.right))
            ]
            assert numeric_multipliers == []


def test_unconsumed_legacy_weighted_rankers_are_removed() -> None:
    ranking = _read("src/trader/recommendation/domain/selection/ranking.py")
    composition = _read("src/trader/recommendation/domain/candidate/composition.py")

    assert "def candidate_score(" not in ranking
    assert "def liquidity_score(" not in composition
