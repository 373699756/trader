"""Strict v16 cache and performance policy parsing."""

from __future__ import annotations

from collections.abc import Mapping

from trader.application.cache import CacheDatasetPolicy, CacheGroupPolicy, CachePolicy
from trader.infra.settings_models import (
    PerformanceBudgetSettings,
    PerformanceMemorySettings,
    PerformanceRoundSettings,
    PerformanceWorkloadSettings,
)
from trader.infra.settings_parser import (
    ConfigurationError,
    boolean,
    integer,
    mapping,
    number,
    require_exact_keys,
    text,
)


def parse_cache_policy(raw: Mapping[str, object]) -> CachePolicy:
    require_exact_keys(
        raw,
        {"policy_version", "datasets", "groups", "total_bytes", "estimator_version"},
        "cache_policy",
    )
    policy_version = text(raw, "policy_version")
    if policy_version != "market_cache_v16":
        raise ConfigurationError("cache_policy.policy_version must be market_cache_v16")
    estimator_version = text(raw, "estimator_version")
    if estimator_version != "canonical_json_utf8_v1":
        raise ConfigurationError("cache_policy.estimator_version must be canonical_json_utf8_v1")
    datasets_raw = mapping(raw, "datasets")
    expected_datasets = {
        "full_market_quotes",
        "candidate_quotes",
        "intraday_minutes",
        "research_success",
        "research_failure",
        "daily_history",
        "security_master_calendar",
        "daily_valuation_financials",
        "history_summary",
        "board_cross_section",
        "candidate_preselection",
        "local_score",
        "competition_group_mapping",
        "raw_deepseek_review",
        "strategy_deepseek_review",
    }
    if set(datasets_raw) != expected_datasets:
        raise ConfigurationError("cache_policy.datasets must match the fixed v16 dataset set")
    datasets: dict[str, CacheDatasetPolicy] = {}
    for name, item in datasets_raw.items():
        if not isinstance(item, dict):
            raise ConfigurationError(f"cache_policy.datasets.{name} must be an object")
        require_exact_keys(
            item,
            {
                "refresh_ttl_seconds",
                "action_max_age_seconds",
                "cadence_task",
                "action_max_age_multiplier",
                "negative_ttl_seconds",
                "capacity",
                "group",
                "persisted",
            },
            f"cache_policy.datasets.{name}",
        )
        try:
            datasets[str(name)] = CacheDatasetPolicy(
                refresh_ttl_seconds=_nullable_number(item, "refresh_ttl_seconds"),
                action_max_age_seconds=_nullable_number(item, "action_max_age_seconds"),
                cadence_task=_nullable_text(item, "cadence_task"),
                action_max_age_multiplier=_nullable_number(item, "action_max_age_multiplier"),
                negative_ttl_seconds=number(item, "negative_ttl_seconds", minimum=0.000001),
                capacity=integer(item, "capacity", minimum=1),
                group=text(item, "group"),
                persisted=boolean(item, "persisted"),
            )
        except ValueError as exc:
            raise ConfigurationError(f"cache_policy.datasets.{name}: {exc}") from exc

    expected_policies = {
        "full_market_quotes": (None, None, "full_market", 3.0, 10.0, 6000, "realtime_observations", False),
        "candidate_quotes": (None, None, "candidate_quotes", 3.0, 3.0, 360, "realtime_observations", False),
        "intraday_minutes": (45.0, 90.0, None, None, 45.0, 360, "history_minutes_research", False),
        "research_success": (600.0, 1200.0, None, None, 60.0, 360, "history_minutes_research", True),
        "research_failure": (60.0, 60.0, None, None, 60.0, 360, "history_minutes_research", False),
        "daily_history": (21600.0, 86400.0, None, None, 60.0, 360, "history_minutes_research", False),
        "security_master_calendar": (
            86400.0,
            86400.0,
            None,
            None,
            300.0,
            6000,
            "history_minutes_research",
            False,
        ),
        "daily_valuation_financials": (
            86400.0,
            86400.0,
            None,
            None,
            300.0,
            360,
            "history_minutes_research",
            False,
        ),
        "history_summary": (21600.0, 86400.0, None, None, 60.0, 360, "history_minutes_research", False),
        "board_cross_section": (86400.0, 86400.0, None, None, 60.0, 24, "board_candidate_scoring", False),
        "candidate_preselection": (86400.0, 86400.0, None, None, 60.0, 4, "board_candidate_scoring", False),
        "local_score": (86400.0, 86400.0, None, None, 60.0, 1080, "board_candidate_scoring", False),
        "competition_group_mapping": (
            86400.0,
            86400.0,
            None,
            None,
            60.0,
            2,
            "board_candidate_scoring",
            False,
        ),
        "raw_deepseek_review": (600.0, 600.0, None, None, 60.0, 2000, "board_candidate_scoring", False),
        "strategy_deepseek_review": (
            600.0,
            600.0,
            None,
            None,
            60.0,
            2000,
            "board_candidate_scoring",
            False,
        ),
    }
    for name, expected in expected_policies.items():
        policy = datasets[name]
        actual = (
            policy.refresh_ttl_seconds,
            policy.action_max_age_seconds,
            policy.cadence_task,
            policy.action_max_age_multiplier,
            policy.negative_ttl_seconds,
            policy.capacity,
            policy.group,
            policy.persisted,
        )
        if actual != expected:
            raise ConfigurationError(f"cache_policy.datasets.{name} must match the fixed v15 policy")

    groups_raw = mapping(raw, "groups")
    expected_groups = {
        "realtime_observations": 64 * 1024 * 1024,
        "history_minutes_research": 128 * 1024 * 1024,
        "board_candidate_scoring": 64 * 1024 * 1024,
    }
    if groups_raw != expected_groups:
        raise ConfigurationError("cache_policy.groups must match the fixed 64/128/64 MiB allocation")
    groups = {name: CacheGroupPolicy(max_bytes=integer(groups_raw, name, minimum=1)) for name in expected_groups}
    try:
        return CachePolicy(
            policy_version=policy_version,
            datasets=datasets,
            groups=groups,
            total_bytes=integer(raw, "total_bytes", minimum=1),
            estimator_version=estimator_version,
        )
    except ValueError as exc:
        raise ConfigurationError(f"cache_policy: {exc}") from exc


def parse_performance_budgets(raw: Mapping[str, object]) -> PerformanceBudgetSettings:
    require_exact_keys(
        raw,
        {
            "schema_version",
            "workload",
            "rounds",
            "latency_p95_ms",
            "data_age_p95_seconds",
            "memory",
            "relative_regression_percent",
        },
        "performance_budgets",
    )
    if integer(raw, "schema_version", minimum=1) != 1:
        raise ConfigurationError("performance_budgets.schema_version must be 1")
    workload_raw = mapping(raw, "workload")
    require_exact_keys(workload_raw, {"market_rows", "candidate_rows"}, "performance_budgets.workload")
    workload = PerformanceWorkloadSettings(
        market_rows=integer(workload_raw, "market_rows", minimum=1),
        candidate_rows=integer(workload_raw, "candidate_rows", minimum=1),
    )
    if workload != PerformanceWorkloadSettings(5500, 360):
        raise ConfigurationError("performance workload must remain fixed at 5500 market rows and 360 candidates")

    rounds_raw = mapping(raw, "rounds")
    require_exact_keys(rounds_raw, {"warmup", "measurement"}, "performance_budgets.rounds")
    rounds = PerformanceRoundSettings(
        warmup=integer(rounds_raw, "warmup", minimum=1),
        measurement=integer(rounds_raw, "measurement", minimum=1),
    )
    if rounds != PerformanceRoundSettings(1, 5):
        raise ConfigurationError("performance rounds must remain fixed at one warmup and five measurements")

    expected_latency = {
        "market_normalization": 800.0,
        "market_merge": 1000.0,
        "canonical_snapshot": 1500.0,
        "board_preselection": 250.0,
        "board_local_scoring": 250.0,
        "three_strategy_board_scoring": 750.0,
        "three_board_wall_clock": 1000.0,
        "global_selection": 100.0,
        "board_ready_to_draft": 500.0,
        "quote_to_draft": 5000.0,
        "deepseek_to_hybrid": 1000.0,
        "sse_delivery": 2000.0,
        "snapshot_api": 200.0,
        "etag_api": 50.0,
        "status_api": 100.0,
    }
    latency = _fixed_positive_number_mapping(raw, "latency_p95_ms", expected_latency)
    expected_age = {
        "topk_critical": 2.0,
        "topk_other": 5.0,
        "candidate_main": 5.0,
        "candidate_other": 10.0,
        "full_market_main": 10.0,
        "full_market_other": 15.0,
    }
    data_age = _fixed_positive_number_mapping(raw, "data_age_p95_seconds", expected_age)

    memory_raw = mapping(raw, "memory")
    require_exact_keys(memory_raw, {"cache_total_bytes", "growth_percent"}, "performance_budgets.memory")
    memory = PerformanceMemorySettings(
        cache_total_bytes=integer(memory_raw, "cache_total_bytes", minimum=1),
        growth_percent=number(memory_raw, "growth_percent", minimum=0.000001),
    )
    if memory != PerformanceMemorySettings(256 * 1024 * 1024, 20.0):
        raise ConfigurationError("performance memory budget must match the fixed v15/v17 contract")
    relative = number(raw, "relative_regression_percent", minimum=0.000001)
    if relative != 5.0:
        raise ConfigurationError("relative_regression_percent must remain fixed at 5")
    return PerformanceBudgetSettings(1, workload, rounds, latency, data_age, memory, relative)


def _fixed_positive_number_mapping(
    raw: Mapping[str, object],
    key: str,
    expected: Mapping[str, float],
) -> dict[str, float]:
    values = mapping(raw, key)
    if set(values) != set(expected):
        raise ConfigurationError(f"performance_budgets.{key} must define the fixed key set")
    parsed = {name: number(values, name, minimum=0.000001) for name in expected}
    if parsed != expected:
        raise ConfigurationError(f"performance_budgets.{key} must match the fixed budget table")
    return parsed


def _nullable_number(raw: Mapping[str, object], key: str) -> float | None:
    if key not in raw:
        raise ConfigurationError(f"{key} is required")
    return None if raw[key] is None else number(raw, key, minimum=0.000001)


def _nullable_text(raw: Mapping[str, object], key: str) -> str | None:
    if key not in raw:
        raise ConfigurationError(f"{key} is required")
    return None if raw[key] is None else text(raw, key)


__all__ = ["parse_cache_policy", "parse_performance_budgets"]
