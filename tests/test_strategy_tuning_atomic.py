from concurrent.futures import ThreadPoolExecutor

from stock_analyzer.strategy_tuning import build_strategy_tuning_plan
from stock_analyzer.strategy_validation import StrategyValidationStore


def test_tuning_run_reuse_is_atomic_for_concurrent_identical_inputs(tmp_path):
    store = StrategyValidationStore(str(tmp_path / "validation.sqlite3"))
    metrics = {
        "day_count": 60,
        "sample_count": 100,
        "outcome_sample_count": 100,
        "real_day_count": 60,
        "replay_day_count": 0,
        "pending_outcome_count": 0,
        "unknown_outcome_count": 0,
        "real_win_rate_primary_net": 55.0,
        "real_avg_primary_return_net": 0.6,
        "real_avg_primary_return_net_ci95_low": 0.1,
        "real_avg_max_drawdown_primary": -2.0,
    }
    plan = build_strategy_tuning_plan(
        "tomorrow_picks",
        metrics,
        [{"signal_date": "2026-07-14", "count": 5}],
        days=60,
    )

    def persist(_):
        return store.save_or_reuse_tuning_run("tomorrow_picks", 60, plan, metrics)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(persist, range(2)))

    assert sorted(item["reused"] for item in results) == [False, True]
    assert len({item["saved"]["id"] for item in results}) == 1
    assert len(store.list_tuning_runs("tomorrow_picks")) == 1


def test_legacy_tuning_run_without_fingerprint_is_regenerated_once(tmp_path):
    store = StrategyValidationStore(str(tmp_path / "validation.sqlite3"))
    metrics = {
        "day_count": 20,
        "sample_count": 20,
        "outcome_sample_count": 20,
        "real_day_count": 20,
        "pending_outcome_count": 0,
        "unknown_outcome_count": 0,
        "real_win_rate_primary_net": 50.0,
        "real_avg_primary_return_net": 0.2,
        "real_avg_primary_return_net_ci95_low": 0.01,
        "real_avg_max_drawdown_primary": -2.0,
    }
    store.save_tuning_run(
        "tomorrow_picks",
        20,
        {"status": "blocked", "can_apply": False, "shadow_mode": True},
        metrics,
    )
    plan = build_strategy_tuning_plan(
        "tomorrow_picks",
        metrics,
        [{"signal_date": "2026-07-14", "count": 5}],
        days=20,
    )

    regenerated = store.save_or_reuse_tuning_run("tomorrow_picks", 20, plan, metrics)
    repeated = store.save_or_reuse_tuning_run("tomorrow_picks", 20, plan, metrics)

    assert not regenerated["reused"]
    assert repeated["reused"]
    assert regenerated["saved"]["id"] == repeated["saved"]["id"]
    assert len(store.list_tuning_runs("tomorrow_picks")) == 2
