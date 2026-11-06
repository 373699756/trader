from __future__ import annotations


def _baseline_identity():
    return {
        "config_version": "config",
        "scoring_profile": "v3",
        "fixture_sha256": "fixture",
        "models_sha256": "model",
        "strategy_contracts_sha256": "strategy",
        "cost_policy_sha256": "cost",
        "performance_budgets_sha256": "budgets",
        "python": "3.11",
        "dependencies": {"numpy": "2.0"},
        "cpu": "fixture-cpu",
        "cpu_count": 8,
        "machine": "x86_64",
        "system": "Linux",
        "kernel": "fixture-kernel",
    }
