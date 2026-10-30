"""Strict cache and performance policy parsing."""

from __future__ import annotations

from collections.abc import Mapping

from trader.infra.cache_contracts import CacheDatasetPolicy, CacheGroupPolicy, CachePolicy
from trader.infra.settings.models import (
    DatabasePerformanceSettings,
    PerformanceBudgetSettings,
    PerformanceMemorySettings,
    PerformanceRoundSettings,
    PerformanceWorkloadSettings,
)
from trader.infra.settings.parser import (
    Boolean,
    ConfigurationError,
    Integer,
    Number,
    RequireExactKeys,
    RequireMapping,
    Text,
)
from trader.recommendation.application.ports.market_data_contracts import (
    LOGICAL_CACHE_LIMIT_BYTES,
    PROCESS_PEAK_RSS_LIMIT_BYTES,
)


def ParseCachePolicy(raw: Mapping[str, object]) -> CachePolicy:
    RequireExactKeys(
        raw,
        {
            "datasets",
            "groups",
            "total_bytes",
            "runtime_reserve_bytes",
            "pool_total_bytes",
            "estimator",
        },
        "cache_policy",
    )
    estimator = Text(raw, "estimator")
    if estimator != "canonical_json_utf8":
        raise ConfigurationError("cache_policy.estimator must be canonical_json_utf8")
    datasets = _parse_cache_datasets(RequireMapping(raw, "datasets"))
    _validate_fixed_cache_datasets(datasets)
    groups = _parse_cache_groups(RequireMapping(raw, "groups"))
    try:
        return CachePolicy(
            datasets=datasets,
            groups=groups,
            total_bytes=Integer(raw, "total_bytes", minimum=1),
            runtime_reserve_bytes=Integer(raw, "runtime_reserve_bytes", minimum=1),
            pool_total_bytes=Integer(raw, "pool_total_bytes", minimum=1),
            estimator=estimator,
        )
    except ValueError as exc:
        raise ConfigurationError(f"cache_policy: {exc}") from exc


def _parse_cache_datasets(datasets_raw: Mapping[str, object]) -> dict[str, CacheDatasetPolicy]:
    expected_datasets = {
        "full_market_quotes",
        "candidate_quotes",
        "intraday_minutes",
        "security_master_calendar",
        "canonical_market_snapshot",
        "canonical_candidate_snapshot",
        "current_quote_index",
        "candidate_feature_batch",
        "hard_filter_batch",
        "board_cross_section",
        "market_cross_section",
        "quote_factor_inputs",
        "candidate_preselection",
        "board_score_batch",
        "global_local_draft",
        "competition_group_mapping",
        "raw_deepseek_review",
        "strategy_deepseek_review",
        "deepseek_seen_codes",
        "published_recommendation_view",
        "published_date_index",
    }
    if set(datasets_raw) != expected_datasets:
        raise ConfigurationError("cache_policy.datasets must match the fixed unified data-plane dataset set")
    datasets: dict[str, CacheDatasetPolicy] = {}
    for name, item in datasets_raw.items():
        if not isinstance(item, dict):
            raise ConfigurationError(f"cache_policy.datasets.{name} must be an object")
        RequireExactKeys(
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
                negative_ttl_seconds=Number(item, "negative_ttl_seconds", minimum=0.000001),
                capacity=Integer(item, "capacity", minimum=1),
                group=Text(item, "group"),
                persisted=Boolean(item, "persisted"),
            )
        except ValueError as exc:
            raise ConfigurationError(f"cache_policy.datasets.{name}: {exc}") from exc

    return datasets


def _validate_fixed_cache_datasets(datasets: Mapping[str, CacheDatasetPolicy]) -> None:
    expected_policies = {
        "full_market_quotes": (None, None, "full_market", 3.0, 10.0, 12000, "observation", False),
        "candidate_quotes": (None, None, "candidate_quotes", 3.0, 3.0, 12000, "observation", False),
        "intraday_minutes": (45.0, 90.0, None, None, 45.0, 12000, "observation", False),
        "security_master_calendar": (
            86400.0,
            86400.0,
            None,
            None,
            300.0,
            12000,
            "observation",
            False,
        ),
        "canonical_market_snapshot": (60.0, 180.0, None, None, 10.0, 3, "canonical_snapshots", False),
        "canonical_candidate_snapshot": (30.0, 90.0, None, None, 10.0, 6, "canonical_snapshots", False),
        "current_quote_index": (30.0, 90.0, None, None, 10.0, 3, "canonical_snapshots", False),
        "candidate_feature_batch": (86400.0, 86400.0, None, None, 60.0, 24, "feature_batches", False),
        "hard_filter_batch": (86400.0, 86400.0, None, None, 60.0, 24, "feature_batches", False),
        "board_cross_section": (86400.0, 86400.0, None, None, 60.0, 24, "feature_batches", False),
        "market_cross_section": (86400.0, 86400.0, None, None, 60.0, 4, "feature_batches", False),
        "quote_factor_inputs": (86400.0, 86400.0, None, None, 60.0, 12000, "feature_batches", False),
        "candidate_preselection": (86400.0, 86400.0, None, None, 60.0, 4, "feature_batches", False),
        "board_score_batch": (86400.0, 86400.0, None, None, 60.0, 24, "local_scoring", False),
        "global_local_draft": (86400.0, 86400.0, None, None, 60.0, 4, "local_scoring", False),
        "competition_group_mapping": (
            86400.0,
            86400.0,
            None,
            None,
            60.0,
            2,
            "feature_batches",
            False,
        ),
        "raw_deepseek_review": (600.0, 600.0, None, None, 60.0, 2000, "review", False),
        "strategy_deepseek_review": (
            600.0,
            600.0,
            None,
            None,
            60.0,
            2000,
            "review",
            False,
        ),
        "deepseek_seen_codes": (600.0, 600.0, None, None, 60.0, 6000, "review", False),
        "published_recommendation_view": (86400.0, 86400.0, None, None, 60.0, 72, "projection", False),
        "published_date_index": (86400.0, 86400.0, None, None, 60.0, 3, "projection", False),
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
            raise ConfigurationError(f"cache_policy.datasets.{name} must match the fixed policy")


def _parse_cache_groups(groups_raw: Mapping[str, object]) -> dict[str, CacheGroupPolicy]:
    expected_groups = {
        "observation": 264 * 1024 * 1024,
        "canonical_snapshots": 116 * 1024 * 1024,
        "feature_batches": 49 * 1024 * 1024,
        "local_scoring": 33 * 1024 * 1024,
        "review": 25 * 1024 * 1024,
        "projection": 25 * 1024 * 1024,
    }
    if groups_raw != expected_groups:
        raise ConfigurationError("cache_policy.groups must match the fixed 264/116/49/33/25/25 MiB allocation")
    return {name: CacheGroupPolicy(max_bytes=Integer(groups_raw, name, minimum=1)) for name in expected_groups}


def ParsePerformanceBudgets(raw: Mapping[str, object]) -> PerformanceBudgetSettings:
    RequireExactKeys(
        raw,
        {
            "workload",
            "rounds",
            "latency_p95_ms",
            "data_age_p95_seconds",
            "memory",
            "relative_regression_percent",
            "database",
        },
        "performance_budgets",
    )
    workload_raw = RequireMapping(raw, "workload")
    RequireExactKeys(workload_raw, {"market_rows", "candidate_rows"}, "performance_budgets.workload")
    workload = PerformanceWorkloadSettings(
        market_rows=Integer(workload_raw, "market_rows", minimum=1),
        candidate_rows=Integer(workload_raw, "candidate_rows", minimum=1),
    )
    if workload != PerformanceWorkloadSettings(5500, 5500):
        raise ConfigurationError("performance workload must remain fixed at 5500 market rows and 5500 scoring inputs")

    rounds_raw = RequireMapping(raw, "rounds")
    RequireExactKeys(rounds_raw, {"warmup", "measurement"}, "performance_budgets.rounds")
    rounds = PerformanceRoundSettings(
        warmup=Integer(rounds_raw, "warmup", minimum=1),
        measurement=Integer(rounds_raw, "measurement", minimum=1),
    )
    if rounds != PerformanceRoundSettings(1, 5):
        raise ConfigurationError("performance rounds must remain fixed at one warmup and five measurements")

    expected_latency = {
        "market_normalization": 250.0,
        "market_merge": 600.0,
        "canonical_snapshot": 900.0,
        "targeted_overlay_commit": 100.0,
        "board_preselection": 5000.0,
        "candidate_union_projection": 100.0,
        "board_local_scoring": 250.0,
        "two_strategy_board_scoring": 750.0,
        "three_board_wall_clock": 1000.0,
        "global_selection": 100.0,
        "board_ready_to_draft": 500.0,
        "quote_to_draft": 5000.0,
        "deepseek_to_hybrid": 1000.0,
        "sse_publish": 100.0,
        "browser_patch_to_paint": 100.0,
        "snapshot_api": 200.0,
        "etag_api": 50.0,
        "dates_api": 100.0,
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

    memory_raw = RequireMapping(raw, "memory")
    RequireExactKeys(
        memory_raw,
        {"cache_logical_bytes", "process_peak_rss_bytes", "growth_percent"},
        "performance_budgets.memory",
    )
    memory = PerformanceMemorySettings(
        cache_logical_bytes=Integer(memory_raw, "cache_logical_bytes", minimum=1),
        process_peak_rss_bytes=Integer(memory_raw, "process_peak_rss_bytes", minimum=1),
        growth_percent=Number(memory_raw, "growth_percent", minimum=0.000001),
    )
    if memory != PerformanceMemorySettings(LOGICAL_CACHE_LIMIT_BYTES, PROCESS_PEAK_RSS_LIMIT_BYTES, 20.0):
        raise ConfigurationError("performance memory budget must match the fixed 512/2048 MiB contract")
    relative = Number(raw, "relative_regression_percent", minimum=0.000001)
    if relative != 5.0:
        raise ConfigurationError("relative_regression_percent must remain fixed at 5")
    database_raw = RequireMapping(raw, "database")
    RequireExactKeys(
        database_raw, {"sqlite_improvement_percent", "parquet_improvement_percent", "minimum_wins"},
        "performance_budgets.database",
    )
    database = DatabasePerformanceSettings(
        Number(database_raw, "sqlite_improvement_percent", minimum=0.000001),
        Number(database_raw, "parquet_improvement_percent", minimum=0.000001),
        Integer(database_raw, "minimum_wins", minimum=1),
    )
    if database != DatabasePerformanceSettings(10.0, 15.0, 4):
        raise ConfigurationError("database performance gates must remain fixed at 10%, 15% and four wins")
    return PerformanceBudgetSettings(workload, rounds, latency, data_age, memory, relative, database)


def _fixed_positive_number_mapping(
    raw: Mapping[str, object],
    key: str,
    expected: Mapping[str, float],
) -> dict[str, float]:
    values = RequireMapping(raw, key)
    if set(values) != set(expected):
        raise ConfigurationError(f"performance_budgets.{key} must define the fixed key set")
    parsed = {name: Number(values, name, minimum=0.000001) for name in expected}
    if parsed != expected:
        raise ConfigurationError(f"performance_budgets.{key} must match the fixed budget table")
    return parsed


def _nullable_number(raw: Mapping[str, object], key: str) -> float | None:
    if key not in raw:
        raise ConfigurationError(f"{key} is required")
    return None if raw[key] is None else Number(raw, key, minimum=0.000001)


def _nullable_text(raw: Mapping[str, object], key: str) -> str | None:
    if key not in raw:
        raise ConfigurationError(f"{key} is required")
    return None if raw[key] is None else Text(raw, key)


__all__ = ["ParseCachePolicy", "ParsePerformanceBudgets"]
