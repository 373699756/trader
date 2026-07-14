from importlib import import_module
import ast
from unittest.mock import patch

from stock_analyzer import config
from stock_analyzer.execution_policy import (
    build_execution_policy,
    execution_cost_for_strategy as execution_cost_for_strategy_direct,
)
from stock_analyzer.normalization import SortedNumericValues, percentile_score
import pytest
from pathlib import Path
from stock_analyzer.backtest import run_alphalite_backtest, run_rolling_alphalite_backtest


def test_public_module_import_matrix_and_all_exports():
    modules = [
        import_module("stock_analyzer.strategy_validation"),
        import_module("stock_analyzer.validation_repository"),
        import_module("stock_analyzer.providers"),
        import_module("stock_analyzer.calibrate"),
        import_module("stock_analyzer.scoring_core.explanations"),
        import_module("stock_analyzer.portfolio_baseline"),
        import_module("stock_analyzer.validation_policy"),
        import_module("stock_analyzer.validation_outcomes"),
        import_module("stock_analyzer.validation_metrics"),
        import_module("stock_analyzer.backtest"),
        import_module("stock_analyzer.scoring_core.base"),
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


def test_no_private_strategy_validation_imports_in_business_modules():
    project_root = Path(__file__).resolve().parents[1]
    for path in sorted((project_root / "stock_analyzer").rglob("*.py")):
        if path.name == "strategy_validation.py":
            continue
        if path.name.endswith("__init__.py"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            module = node.module or ""
            if module != "strategy_validation" and module != "stock_analyzer.strategy_validation":
                continue
            names = [alias.name for alias in node.names if alias.name != "*"]
            private = [name for name in names if name.startswith("_")]
            assert not private, "{} has private strategy_validation import: {}".format(path, names)


def test_strategy_validation_imports_must_be_whitelisted():
    project_root = Path(__file__).resolve().parents[1]
    allowed_imports = {
        "app_support": {"StrategyValidationStore"},
        "daily_job": {"StrategyValidationStore"},
        "calibrate": {"StrategyValidationStore"},
        "validation_audit_cli": {"StrategyValidationStore"},
        "app_container": {"StrategyValidationStore"},
        "jobs": {"StrategyValidationStore"},
        "validation_outcomes": {"compute_outcome"},
    }
    for path in sorted((project_root / "stock_analyzer").rglob("*.py")):
        if path.name == "strategy_validation.py" or path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module_name = path.name[:-3]
        names = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            module = node.module or ""
            if module != "strategy_validation" and module != "stock_analyzer.strategy_validation":
                continue
            names.update(alias.name for alias in node.names if alias.name != "*")
        if not names:
            continue
        unexpected = names - allowed_imports.get(module_name, set())
        assert not unexpected, "{} imports non-whitelisted symbols from strategy_validation: {}".format(
            path, sorted(unexpected)
        )


def test_validation_repository_duplicate_repository_aliases():
    from stock_analyzer import validation_repository as vr

    assert vr.PortfolioRepository is vr.TuningRepository
    assert vr.ExperimentRepository is vr.ResearchRepository


def test_validation_policy_execution_cost_for_strategy_requires_context():
    from stock_analyzer.validation_policy import execution_cost_for_strategy
    from stock_analyzer import config

    with pytest.raises(ValueError):
        execution_cost_for_strategy({}, "")

    liquid = execution_cost_for_strategy({"strategy_name": "tomorrow_picks", "turnover": 1_500_000_000}, "")
    illiquid = execution_cost_for_strategy({"strategy_name": "tomorrow_picks", "turnover": 50_000_000}, "")
    assert illiquid > liquid
    with patch.object(config, "ENABLE_TAIL_AUCTION_SLIPPAGE", False), patch.object(config, "ENABLE_MARKET_IMPACT", False):
        custom = execution_cost_for_strategy({"turnover": 100_000_000, "strategy_name": "tomorrow_picks"}, "", policy={"policy_version": "x", "cost": {"fee_round_trip_pct": 0.25, "liquidity_slippage_pct": {"turnover_ge_1b": 0.0, "turnover_ge_300m": 0.0, "turnover_ge_100m": 0.0, "turnover_lt_100m": 0.0}, "tail_auction": {"enabled": False, "liquidity_ratio": 0.05, "max_extra_pct": 0.8}, "market_impact": {"enabled": False, "coefficient": 0.0, "max_cost_pct": 5.0}}, "portfolio": {"capital": 1000000.0}, "schema_version": 3, "strategy_name": "tomorrow_picks", "market": "", "entry": {}, "exit": {}})
    assert custom == execution_cost_for_strategy({"turnover": 100_000_000, "strategy_name": "tomorrow_picks"}, "tomorrow_picks")


def test_alphalite_backtest_records_cost_policy_version():
    from stock_analyzer import config

    with patch.object(config, "ENABLE_TAIL_AUCTION_SLIPPAGE", False), patch.object(config, "ENABLE_MARKET_IMPACT", False):
        result = run_alphalite_backtest(
            {
                "600001": __import__("pandas").DataFrame(
                    {
                        "trade_date": ["20240101", "20240102", "20240103", "20240104", "20240105", "20240106", "20240107", "20240108"],
                        "price": [10.0, 10.2, 10.1, 10.3, 10.7, 10.6, 10.9, 11.2],
                        "open": [10.0, 10.1, 10.1, 10.2, 10.4, 10.5, 10.8, 10.9],
                        "high": [10.1, 10.3, 10.2, 10.4, 10.8, 10.7, 11.0, 11.3],
                        "turnover": [9_000_000, 9_500_000, 10_000_000, 10_500_000, 11_000_000, 11_500_000, 12_000_000, 12_500_000],
                    }
                )
            },
            top_k=1,
            holding_days=3,
        )
    assert result["metrics"]["cost_policy_version"] != "override"
    assert result["selected"][0]["trade_cost_policy_version"] == result["metrics"]["cost_policy_version"]


def test_rolling_alphalite_backtest_records_cost_policy_version():
    with patch.object(config, "ENABLE_TAIL_AUCTION_SLIPPAGE", False), patch.object(config, "ENABLE_MARKET_IMPACT", False):
        result = run_rolling_alphalite_backtest(
            {
                "600001": __import__("pandas").DataFrame(
                    {
                        "trade_date": ["202401{:02d}".format(i + 1) for i in range(90)],
                        "price": [10 + i * 0.05 for i in range(90)],
                        "high": [10 + i * 0.05 for i in range(90)],
                        "turnover": [10_000_000 + i * 10_000 for i in range(90)],
                    }
                )
            },
            top_k=1,
            holding_days=3,
            lookback_days=30,
            rebalance_step=5,
        )
    assert result["ok"]
    assert result["metrics"]["cost_policy_version"] != "override"
    assert result["trades"][0]["selected"][0]["trade_cost_policy_version"] == result["metrics"]["cost_policy_version"]
