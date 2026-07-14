from importlib import import_module
from unittest.mock import patch

from stock_analyzer import config
from stock_analyzer.execution_policy import (
    build_execution_policy,
    execution_cost_for_strategy as execution_cost_for_strategy_direct,
)
from stock_analyzer.normalization import SortedNumericValues, percentile_score
import pytest
from pathlib import Path
import re


def test_public_module_import_matrix_and_all_exports():
    modules = [
        import_module("stock_analyzer.strategy_validation"),
        import_module("stock_analyzer.validation_repository"),
        import_module("stock_analyzer.providers"),
        import_module("stock_analyzer.calibrate"),
        import_module("stock_analyzer.scoring_core.explanations"),
    ]
    for module in modules:
        exports = getattr(module, "__all__", [])
        for symbol in exports:
            assert hasattr(module, symbol), "{} missing export {!r}".format(module.__name__, symbol)


def test_execution_cost_for_strategy_requires_explicit_context():
    with patch.object(config, "ENABLE_TAIL_AUCTION_SLIPPAGE", False), patch.object(
        config, "ENABLE_MARKET_IMPACT", False
    ):
        with pytest.raises(ValueError):
            execution_cost_for_strategy_direct({}, "", None)

        liquid = execution_cost_for_strategy_direct(
            {"strategy_name": "tomorrow_picks", "turnover": 2_000_000_000},
            "tomorrow_picks",
        )
        illiquid = execution_cost_for_strategy_direct(
            {"strategy_name": "tomorrow_picks", "turnover": 10_000_000},
            "tomorrow_picks",
        )
    assert illiquid > liquid


def test_execution_cost_for_strategy_uses_policy_and_still_follows_compatibility_defaults():
    with patch.object(config, "ENABLE_TAIL_AUCTION_SLIPPAGE", False), patch.object(
        config, "ENABLE_MARKET_IMPACT", False
    ):
        policy = build_execution_policy("tomorrow_picks")
        direct = execution_cost_for_strategy_direct(
            {"turnover": 100_000_000, "strategy_name": "swing_picks"},
            "tomorrow_picks",
            policy=policy,
        )
        policy_override = execution_cost_for_strategy_direct(
            {"turnover": 100_000_000},
            "",
            policy=policy,
        )
    assert direct > 0.0
    assert policy_override == direct


def test_percentile_score_preserves_ties_and_empty_behavior_with_pre_sorted_values():
    values = SortedNumericValues([1.0, 2.0, 2.0, 2.0, 5.0])
    assert percentile_score(2.0, values) == 80.0
    assert percentile_score(2.0, values, higher_is_better=False) == 20.0
    assert percentile_score(0.5, values) == 20.0
    assert percentile_score(2.0, []) == 50.0


def test_percentile_score_pre_sorted_matches_iterable_path():
    values = [1.0, 2.0, 2.0, 4.0, float("inf"), float("nan")]
    sorted_values = SortedNumericValues(values)
    assert percentile_score(2.0, values) == percentile_score(2.0, sorted_values)
    assert percentile_score(3.0, values) == percentile_score(3.0, sorted_values)


def test_no_top_level_private_strategy_validation_imports_in_business_modules():
    project_root = Path(__file__).resolve().parents[1]
    for path in sorted((project_root / "stock_analyzer").rglob("*.py")):
        if path.name == "strategy_validation.py":
            continue
        if path.name.endswith("__init__.py"):
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith(" "):
                continue
            match = re.match(r"^from \\.?strategy_validation import (.+)$", line)
            if not match:
                continue
            names = [item.strip() for item in match.group(1).split(",")]
            private = [name for name in names if name.startswith("_")]
            assert not private, "{} has private strategy_validation import: {}".format(path, names)
