from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _function(relative: str, name: str) -> ast.FunctionDef:
    tree = ast.parse(_read(relative))
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
    validation = _read("src/trader/infra/settings/strategy_validation.py")

    assert "_FIXED_DIMENSION_WEIGHTS" not in validation
    assert "_FIXED_BOARD_CANDIDATE_WEIGHTS" not in validation
    assert "_FIXED_BOARD_LOCAL_WEIGHTS" not in validation
    assert "_validate_fixed_vector" not in validation
    assert "fixed at 0.68" not in validation


def test_domain_weight_consumers_do_not_multiply_by_numeric_literals() -> None:
    scoring_path = "src/trader/domain/recommendation/scoring/scoring.py"
    for function_name in ("board_candidate_components", "score_board_strategy"):
        function = _function(scoring_path, function_name)
        numeric_multipliers = [
            node
            for node in ast.walk(function)
            if isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Mult)
            and (_is_weight_literal(node.left) or _is_weight_literal(node.right))
        ]
        assert numeric_multipliers == []

    fusion = _read("src/trader/domain/recommendation/risk_fusion/fusion.py")
    assert "FusionPolicy(local_weight: float = 0.68" not in fusion
    assert "fusion weights are fixed at 0.68/0.32" not in fusion

    for relative, function_names in (
        ("src/trader/infra/market_data/normalization/features.py", ("_raw_features",)),
        ("src/trader/domain/market/research.py", ("_industry_policy_score", "_protection_score")),
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
    ranking = _read("src/trader/domain/recommendation/selection/ranking.py")
    composition = _read("src/trader/domain/recommendation/strategies/composition.py")

    assert "def candidate_score(" not in ranking
    assert "def liquidity_score(" not in composition
