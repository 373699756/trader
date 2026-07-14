import pytest

from stock_analyzer.experiment_registry import list_experiments, validate_experiment
from stock_analyzer.portfolio_baseline import _paired_daily_comparison
from stock_analyzer.validation_statistics import (
    paired_increment_statistics,
    unified_experiment_fdr,
)


def test_registry_records_cover_plan_fields_and_reject_implicit_sample_type():
    records = list_experiments()

    assert len(records) >= 2
    assert all(validate_experiment(record)["trial_count"] >= 1 for record in records)
    with pytest.raises(ValueError, match="missing experiment fields"):
        validate_experiment(
            {
                "experiment_id": "implicit-real",
                "hypothesis": "missing provenance must not become real forward",
            }
        )


def test_unified_fdr_counts_unreported_declared_trials():
    result = unified_experiment_fdr(
        [
            {
                "experiment_id": "A",
                "experiment_family": "family",
                "trial_count": 3,
                "result": {"p_values": [0.001]},
            },
            {
                "experiment_id": "B",
                "experiment_family": "family",
                "trial_count": 2,
                "result": {"p_values": [0.05, 0.9]},
            },
        ],
        q=0.1,
        experiment_family="family",
    )

    assert result["scope"] == "full_experiment_family"
    assert result["declared_trial_count"] == 5
    assert result["reported_trial_count"] == 3
    assert result["rejected_experiment_ids"] == ["A"]


def test_paired_daily_statistics_use_increment_not_two_unpaired_means():
    baseline = [0.4, -0.2, 0.1, 0.3, -0.1, 0.2]
    challenger = [value + 0.25 for value in baseline]

    result = paired_increment_statistics(
        baseline,
        challenger,
        samples=500,
        trial_count=4,
    )

    assert result["method"] == "paired_daily_moving_block_bootstrap"
    assert result["mean_incremental_return_pct"] == 0.25
    assert result["increment_ci95_low"] == 0.25
    assert result["confidence_interval_passed"]
    assert result["dsr"]["passed"]


def test_portfolio_pairing_emits_dated_increments_and_unified_gate():
    daily = []
    for day in range(1, 61):
        daily.append(
            {
                "signal_date": "2026-{:03d}".format(day),
                "groups": {
                    "frozen_rule_top_k": {"status": "settled", "net_return_pct": 0.1},
                    "model_top_k": {"status": "settled", "net_return_pct": 0.3},
                },
            }
        )

    result = _paired_daily_comparison(daily, trial_count=2)

    assert len(result["increments"]) == 60
    assert result["statistics"]["mean_incremental_return_pct"] == 0.2
    assert result["promotion_gate"]["status"] == "passed"
