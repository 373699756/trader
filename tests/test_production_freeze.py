import json
import os
import subprocess
import sys
from unittest.mock import patch

import pandas as pd
import pytest

from stock_analyzer import app_runtime_support, config
from stock_analyzer.deepseek_rules import apply_rule_penalty
from stock_analyzer.experiment_registry import list_experiments, register_experiment
from stock_analyzer.production_baseline import (
    attach_generation_provenance,
    production_baseline_status,
    verify_generation_replay,
)
from stock_analyzer.recommendation_snapshot import load_recommendation_snapshot, save_recommendation_snapshot
from stock_analyzer.strategy_validation import _top_k_sensitivity
from stock_analyzer.scoring_core import base as scoring_base


EXPERIMENT_SWITCHES = (
    "ENABLE_EXPECTED_RETURN_RANKING",
    "ENABLE_INTERACTION_TERMS",
    "ENABLE_REGIME_SPECIFIC_WEIGHTS",
    "ENABLE_META_LABELING",
    "META_LABELING_ENFORCE_ACTION",
    "ENABLE_EVENT_ALPHA",
    "ENABLE_ENSEMBLE",
)


def test_production_freeze_overrides_environment_takeover_switches():
    environment = dict(os.environ)
    environment.update({name: "1" for name in EXPERIMENT_SWITCHES})
    environment["TOMORROW_TOP_N"] = "10"
    script = (
        "import json; from stock_analyzer import config; "
        "print(json.dumps({'switches': {name: getattr(config, name) for name in %r}, "
        "'top_k': config.TOMORROW_TOP_N, 'shadow': config.DEEPSEEK_SHADOW_ONLY}))"
    ) % (EXPERIMENT_SWITCHES,)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=os.getcwd(),
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)

    assert payload["top_k"] == 5
    assert payload["shadow"] is True
    assert all(value is False for value in payload["switches"].values())


def test_frozen_manifest_matches_effective_runtime():
    status = production_baseline_status()

    assert status["status"] == "frozen"
    assert status["drift"] == []
    assert status["research"]["first_strategy"] == "tomorrow_picks"
    assert status["research"]["production_top_k"] == 5
    assert status["research"]["sensitivity_top_k"] == [3, 5, 10]
    assert status["ranking"]["tomorrow_picks"]["field"] == "score"


def test_regime_specific_weight_switch_controls_actual_scoring_multiplier():
    regime = {"level": "risk_on"}
    with patch.object(config, "ENABLE_REGIME_SPECIFIC_WEIGHTS", False):
        assert scoring_base._regime_weight("momentum", regime) == 1.0
    with patch.object(config, "ENABLE_REGIME_SPECIFIC_WEIGHTS", True):
        assert scoring_base._regime_weight("momentum", regime) == 1.12


def test_deepseek_shadow_keeps_production_order_and_action():
    rows = [
        {"code": "600001", "rank": 1, "score": 90, "tier": "primary_watch", "execution_allowed": True},
        {"code": "600002", "rank": 2, "score": 80, "tier": "primary_watch", "execution_allowed": True},
    ]
    reviewed = [
        {
            **rows[1],
            "rank": 1,
            "score": 99,
            "local_rank": 2,
            "deepseek_rank_score": 99,
            "deepseek_action": "priority",
            "rerank_source": "deepseek",
        }
    ]
    filtered = {
        **rows[0],
        "local_rank": 1,
        "deepseek_rank_score": 10,
        "deepseek_action": "avoid",
        "deepseek_veto": True,
        "deepseek_filter_reason": "deepseek_veto",
    }
    with patch.object(config, "ENABLE_DEEPSEEK_RUNTIME", True), patch.object(
        app_runtime_support,
        "scheduled_deepseek_decision",
        return_value={"enabled": False},
    ), patch.object(
        app_runtime_support,
        "rerank_candidates",
        return_value=(reviewed, {"status": "ok", "filtered": 1, "filtered_rows": [filtered]}),
    ):
        result, meta = app_runtime_support.apply_deepseek_rerank("tomorrow_picks", rows, "all")

    assert [(row["code"], row["rank"], row["score"]) for row in result] == [
        ("600001", 1, 90),
        ("600002", 2, 80),
    ]
    assert all(row["tier"] == "primary_watch" and row["execution_allowed"] for row in result)
    assert result[0]["deepseek_shadow_filtered"] is True
    assert result[1]["deepseek_shadow_rank"] == 1
    assert meta["mode"] == "shadow_only"
    assert meta["production_applied"] is False


def test_deepseek_schedule_cache_reuse_is_shadow_only():
    rows = [
        {"code": "600001", "rank": 1, "score": 90, "tier": "primary_watch", "execution_allowed": True},
        {"code": "600002", "rank": 2, "score": 80, "tier": "primary_watch", "execution_allowed": True},
    ]
    reused_rows = [
        {
            **rows[1],
            "rank": 1,
            "score": 99,
            "local_rank": 2,
            "deepseek_rank_score": 99,
            "deepseek_action": "priority",
            "rerank_source": "deepseek_schedule_cache",
        },
        {
            **rows[0],
            "rank": 2,
            "score": 10,
            "local_rank": 1,
            "deepseek_rank_score": 10,
            "deepseek_action": "avoid",
            "rerank_source": "deepseek_schedule_cache",
        },
    ]

    with patch.object(config, "ENABLE_DEEPSEEK_RUNTIME", True), patch.object(
        app_runtime_support,
        "scheduled_deepseek_decision",
        return_value={"enabled": True, "allow_call": False, "reuse": True},
    ), patch.object(
        app_runtime_support,
        "reuse_scheduled_deepseek_result",
        return_value=(reused_rows, {"status": "schedule_cache_hit", "source": "deepseek_schedule_cache"}),
    ):
        result, meta = app_runtime_support.apply_deepseek_rerank("tomorrow_picks", rows, "all")

    assert [(row["code"], row["rank"], row["score"]) for row in result] == [
        ("600001", 1, 90),
        ("600002", 2, 80),
    ]
    assert result[0]["deepseek_shadow_rank"] == 2
    assert result[1]["deepseek_shadow_rank"] == 1
    assert meta["mode"] == "shadow_only"
    assert meta["production_applied"] is False


def test_deepseek_batch_schedule_cache_reuse_is_shadow_only():
    rows = [
        {"code": "600001", "rank": 1, "score": 90, "tier": "primary_watch", "execution_allowed": True},
        {"code": "600002", "rank": 2, "score": 80, "tier": "primary_watch", "execution_allowed": True},
    ]
    reused_rows = [
        {
            **rows[1],
            "rank": 1,
            "score": 99,
            "deepseek_rank_score": 99,
            "deepseek_action": "priority",
        },
        {
            **rows[0],
            "rank": 2,
            "score": 10,
            "deepseek_rank_score": 10,
            "deepseek_action": "avoid",
        },
    ]

    with patch.object(config, "ENABLE_DEEPSEEK_RUNTIME", True), patch.object(
        app_runtime_support,
        "scheduled_deepseek_decision",
        return_value={"enabled": True, "allow_call": False, "reuse": True},
    ), patch.object(
        app_runtime_support,
        "reuse_scheduled_deepseek_result",
        return_value=(reused_rows, {"status": "schedule_cache_hit", "source": "deepseek_schedule_cache"}),
    ), patch.object(
        app_runtime_support,
        "rerank_candidates_batch",
        side_effect=AssertionError("cache reuse should not call DeepSeek"),
    ):
        result, meta = app_runtime_support.apply_deepseek_rerank_batch({"tomorrow_picks": rows}, "all")

    result_rows = result["tomorrow_picks"]
    assert [(row["code"], row["rank"], row["score"]) for row in result_rows] == [
        ("600001", 1, 90),
        ("600002", 2, 80),
    ]
    assert result_rows[0]["deepseek_shadow_rank"] == 2
    assert result_rows[1]["deepseek_shadow_rank"] == 1
    assert meta["tomorrow_picks"]["mode"] == "shadow_only"
    assert meta["tomorrow_picks"]["production_applied"] is False


def test_deepseek_rules_are_shadow_only(tmp_path):
    weights_path = tmp_path / "weights.json"
    weights_path.write_text(
        json.dumps(
            {
                "deepseek_rules": {
                    "tomorrow_picks": [
                        {"field": "pct_chg", "operator": ">", "threshold": 3, "penalty": 20}
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    row = {"code": "600001", "score": 80, "pct_chg": 4}
    with patch.object(config, "WEIGHTS_OVERRIDE_PATH", str(weights_path)):
        result = apply_rule_penalty("tomorrow_picks", row)

    assert result["score"] == 80
    assert result["deepseek_rule_shadow_penalty"] == 20
    assert result["deepseek_rule_shadow_only"] is True


def test_generation_replay_is_stable_for_same_input():
    candidates = pd.DataFrame(
        [
            {"code": "600001", "price": 10, "pct_chg": 2},
            {"code": "600002", "price": 12, "pct_chg": 1},
        ]
    )
    candidates.attrs["quote_timestamp"] = "2026-07-12T15:00:00"
    rows = [{"code": "600001", "rank": 1, "score": 88, "tier": "primary_watch"}]
    meta = {
        "generated_at": "2026-07-12T15:00:01",
        "strategy_version": config.TOMORROW_STRATEGY_VERSION,
        "top_n": 5,
        "market_filter": "all",
        "market_regime": {"level": "balanced", "score": 50},
    }
    generation = attach_generation_provenance(meta, "tomorrow_picks", rows, candidates)
    replay_meta = {**meta, "generated_at": "2026-07-13T10:00:00"}

    replay = verify_generation_replay(generation, rows, candidates, replay_meta)
    changed = verify_generation_replay(generation, [{**rows[0], "score": 87}], candidates, replay_meta)

    assert replay["status"] == "reproduced"
    assert changed["status"] == "mismatch"
    assert changed["mismatches"][0]["field"] == "output_fingerprint"


def test_recommendation_snapshot_rejects_other_baseline(tmp_path):
    snapshot_path = tmp_path / "recommendations.json"
    save_recommendation_snapshot(
        str(snapshot_path),
        {"meta": {"market_filter": "all", "top_n": 5}, "recommendations": {}},
    )

    matched = load_recommendation_snapshot(str(snapshot_path), expected_market="all", expected_top_n=5)
    mismatch = load_recommendation_snapshot(
        str(snapshot_path),
        expected_market="all",
        expected_top_n=5,
        expected_baseline_id="different_baseline",
    )

    assert matched["ok"]
    assert mismatch["status"] == "baseline_mismatch"


def test_experiment_registry_requires_one_change_and_fixed_top_k(tmp_path):
    path = tmp_path / "registry.jsonl"
    record = {
        "experiment_id": "P0-TEST-1",
        "hypothesis": "One change improves paired OOS net return.",
        "unique_change": "Change one threshold.",
        "training_window": {"start": "2026-01-01", "end": "2026-03-31"},
        "test_window": {"start": "2026-04-01", "end": "2026-06-30"},
        "primary_metric": "daily_incremental_net_return",
        "risk_constraints": ["Sortino must not decline"],
        "experiment_family": "threshold",
        "result": {"status": "pending"},
        "decision": "pending",
    }
    saved = register_experiment(record, str(path))

    assert saved["top_k"] == {"production": 5, "sensitivity": [3, 5, 10], "selection_locked": True}
    assert list_experiments(str(path))[0]["experiment_id"] == "P0-TEST-1"
    with pytest.raises(ValueError, match="exactly one change"):
        register_experiment({**record, "experiment_id": "P0-TEST-2", "unique_change": ["a", "b"]}, str(path))
    with pytest.raises(ValueError, match="frozen at 5"):
        register_experiment(
            {**record, "experiment_id": "P0-TEST-3", "top_k": {"production": 10, "sensitivity": [3, 5, 10]}},
            str(path),
        )


def test_top_k_sensitivity_is_paired_and_never_selects_best_k():
    rows = []
    for signal_date, returns, count in (
        ("2026-07-10", [3, 2, 1, 0, -1, -2, -3, -4, -5, -6], 10),
        ("2026-07-11", [1, 1, 1, 1, 1], 5),
    ):
        for rank in range(1, count + 1):
            rows.append(
                {
                    "signal_date": signal_date,
                    "rank": rank,
                    "_primary_ready": True,
                    "_primary_return_net": returns[rank - 1],
                }
            )

    result = _top_k_sensitivity(rows, (3, 5, 10))
    reports = {report["top_k"]: report for report in result["reports"]}

    assert result["production_top_k"] == 5
    assert reports[3]["day_count"] == 2
    assert reports[5]["day_count"] == 2
    assert reports[10]["day_count"] == 1
    assert reports[5]["production"] is True
    assert all(report["selection_locked"] for report in reports.values())
    assert "cannot be selected" in result["selection_policy"]
