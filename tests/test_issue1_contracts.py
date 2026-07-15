from importlib import import_module
import ast
import json
import os
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


def test_percentile_score_constant_samples_are_neutral():
    assert percentile_score(0.0, [0.0, 0.0, 0.0]) == 50.0
    assert percentile_score(3.0, [3.0]) == 50.0
    assert percentile_score(0.0, [0.0, 0.0], higher_is_better=False) == 50.0


def test_percentile_score_pre_sorted_matches_iterable_path():
    values = [1.0, 2.0, 2.0, 4.0, float("inf"), float("nan")]
    sorted_values = SortedNumericValues(values)
    assert percentile_score(2.0, values) == percentile_score(2.0, sorted_values)
    assert percentile_score(3.0, values) == percentile_score(3.0, sorted_values)


def test_score_context_excludes_unready_historical_rows():
    import pandas as pd

    from stock_analyzer.scoring_core.scoring_math import _score_context

    frame = pd.DataFrame(
        {
            "code": ["600001", "600002", "600003"],
            "alphalite_factor_ready": [1, 0, 1],
            "ret_20d": [2.0, 999.0, 4.0],
            "sixty_day_pct": [5.0, 888.0, 10.0],
            "pct_chg": [1.0, 2.0, 3.0],
        }
    )
    context = _score_context(frame, {})
    assert list(context["ret_20d_values"]) == [2.0, 4.0]
    assert list(context["sixty_day_values"]) == [5.0, 10.0]
    assert list(context["pct_values"]) == [1.0, 2.0, 3.0]

    one_ready = frame.assign(alphalite_factor_ready=[1, 0, 0])
    sparse_context = _score_context(one_ready, {})
    assert list(sparse_context["ret_20d_values"]) == []
    assert list(sparse_context["sixty_day_values"]) == []


def test_unready_row_cannot_rank_on_zero_history_placeholder():
    from stock_analyzer.scoring_core.scoring_math import _optional_factor_score

    assert _optional_factor_score(0.0, [-8.0, -3.0], available=False) == 50.0
    assert _optional_factor_score(0.0, [-8.0, -3.0], available=True) == 100.0


def test_stability_membership_keeps_current_selection_order(tmp_path):
    from stock_analyzer.stability import TopKDropoutTracker

    tracker = TopKDropoutTracker(str(tmp_path / "state.json"), keep_k=2, buffer_k=3)
    tracker.update(
        "today_term",
        [
            {"code": "600001", "score": 90.0, "selection_rank": 1},
            {"code": "600002", "score": 80.0, "selection_rank": 2},
        ],
    )
    current = tracker.update(
        "today_term",
        [
            {"code": "600003", "score": 99.0, "selection_rank": 1},
            {"code": "600002", "score": 95.0, "selection_rank": 2},
            {"code": "600001", "score": 70.0, "selection_rank": 3},
        ],
    )

    assert [row["code"] for row in current["rows"]] == ["600002", "600001"]
    assert [row["selection_rank"] for row in current["rows"]] == [2, 3]
    assert [row["display_rank"] for row in current["rows"]] == [1, 2]


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
        "data_health": {"StrategyValidationStore"},
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
        policy = {"policy_version": "x", "cost": {"fee_round_trip_pct": 0.25, "liquidity_slippage_pct": {"turnover_ge_1b": 0.0, "turnover_ge_300m": 0.0, "turnover_ge_100m": 0.0, "turnover_lt_100m": 0.0}, "tail_auction": {"enabled": False, "liquidity_ratio": 0.05, "max_extra_pct": 0.8}, "market_impact": {"enabled": False, "coefficient": 0.0, "max_cost_pct": 5.0}}, "portfolio": {"capital": 1000000.0}, "schema_version": 3, "strategy_name": "tomorrow_picks", "market": "", "entry": {}, "exit": {}}
        custom = execution_cost_for_strategy(
            {"turnover": 100_000_000, "strategy_name": "tomorrow_picks"},
            "",
            policy=policy,
        )
        explicit = execution_cost_for_strategy(
            {"turnover": 100_000_000, "strategy_name": "tomorrow_picks"},
            "tomorrow_picks",
            policy=policy,
        )
    assert custom == explicit == 0.25


def test_alphalite_backtest_records_cost_policy_version():
    from stock_analyzer import config

    row_count = 35
    with patch.object(config, "ENABLE_TAIL_AUCTION_SLIPPAGE", False), patch.object(config, "ENABLE_MARKET_IMPACT", False):
        result = run_alphalite_backtest(
            {
                "600001": __import__("pandas").DataFrame(
                    {
                        "trade_date": ["202401{:02d}".format(index + 1) for index in range(row_count)],
                        "price": [10.0 + index * 0.05 for index in range(row_count)],
                        "open": [9.98 + index * 0.05 for index in range(row_count)],
                        "high": [10.1 + index * 0.05 for index in range(row_count)],
                        "turnover": [9_000_000 + index * 500_000 for index in range(row_count)],
                    }
                )
            },
            top_k=1,
            holding_days=3,
        )
    assert result["ok"]
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


def test_factor_ic_artifact_is_loaded_once_per_scoring_context(tmp_path):
    from stock_analyzer.scoring_core import scoring_math

    path = tmp_path / "factor_ic.json"
    path.write_text(
        json.dumps(
            {
                "ic": {
                    "momentum_score": {
                        "ic": 0.4,
                        "sample_count": 50,
                        "status": "ok",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    frame = __import__("pandas").DataFrame(
        {
            "pct_chg": [1.0, 2.0, 3.0],
            "speed": [0.1, 0.2, 0.3],
            "turnover": [100_000_000, 200_000_000, 300_000_000],
        }
    )
    components = {
        "liquidity_score": 50.0,
        "momentum_score": 60.0,
        "trend_score": 55.0,
        "historical_edge_score": 50.0,
        "execution_score": 50.0,
        "tail_setup_score": 50.0,
        "risk_penalty": 0.0,
        "regime_bonus": 0.0,
    }
    scoring_math._FACTOR_IC_CACHE.update(
        {"path": None, "mtime_ns": None, "payload": {}}
    )

    with patch.object(config, "ENABLE_FACTOR_IC_WEIGHTING", True), patch.object(
        config,
        "FACTOR_IC_PATH",
        str(path),
    ), patch.object(config, "FACTOR_IC_MIN_SAMPLES", 1), patch(
        "stock_analyzer.scoring_core.scoring_math.os.stat",
        wraps=os.stat,
    ) as stat_call:
        context = scoring_math._score_context(frame, {})
        for _ in range(20):
            scoring_math._combine_details(
                components,
                "tomorrow_picks",
                factor_ic_payload=context["factor_ic_payload"],
            )

    assert stat_call.call_count == 1
