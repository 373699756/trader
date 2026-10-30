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

_CACHE_DATASETS = frozenset(
    {
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
)
_CACHE_GROUPS = frozenset(
    {"observation", "canonical_snapshots", "feature_batches", "local_scoring", "review", "projection"}
)
_LATENCY_BUDGETS = frozenset(
    {
        "market_normalization",
        "market_merge",
        "canonical_snapshot",
        "targeted_overlay_commit",
        "board_preselection",
        "candidate_union_projection",
        "board_local_scoring",
        "two_strategy_board_scoring",
        "three_board_wall_clock",
        "global_selection",
        "board_ready_to_draft",
        "quote_to_draft",
        "deepseek_to_hybrid",
        "sse_publish",
        "browser_patch_to_paint",
        "snapshot_api",
        "etag_api",
        "dates_api",
        "status_api",
    }
)
_DATA_AGE_BUDGETS = frozenset(
    {"topk_critical", "topk_other", "candidate_main", "candidate_other", "full_market_main", "full_market_other"}
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
    if set(datasets_raw) != _CACHE_DATASETS:
        raise ConfigurationError("cache_policy.datasets must define the complete unified data-plane dataset set")
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


def _parse_cache_groups(groups_raw: Mapping[str, object]) -> dict[str, CacheGroupPolicy]:
    if set(groups_raw) != _CACHE_GROUPS:
        raise ConfigurationError("cache_policy.groups must define the complete cache group set")
    return {name: CacheGroupPolicy(max_bytes=Integer(groups_raw, name, minimum=1)) for name in _CACHE_GROUPS}


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
    if workload.candidate_rows > workload.market_rows:
        raise ConfigurationError("performance candidate rows cannot exceed market rows")

    rounds_raw = RequireMapping(raw, "rounds")
    RequireExactKeys(rounds_raw, {"warmup", "measurement"}, "performance_budgets.rounds")
    rounds = PerformanceRoundSettings(
        warmup=Integer(rounds_raw, "warmup", minimum=1),
        measurement=Integer(rounds_raw, "measurement", minimum=1),
    )
    latency = _positive_number_mapping(raw, "latency_p95_ms", _LATENCY_BUDGETS)
    data_age = _positive_number_mapping(raw, "data_age_p95_seconds", _DATA_AGE_BUDGETS)

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
    relative = Number(raw, "relative_regression_percent", minimum=0.000001)
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
    return PerformanceBudgetSettings(workload, rounds, latency, data_age, memory, relative, database)


def _positive_number_mapping(
    raw: Mapping[str, object],
    key: str,
    expected_names: frozenset[str],
) -> dict[str, float]:
    values = RequireMapping(raw, key)
    if set(values) != expected_names:
        raise ConfigurationError(f"performance_budgets.{key} must define the complete metric set")
    return {name: Number(values, name, minimum=0.000001) for name in expected_names}


def _nullable_number(raw: Mapping[str, object], key: str) -> float | None:
    if key not in raw:
        raise ConfigurationError(f"{key} is required")
    return None if raw[key] is None else Number(raw, key, minimum=0.000001)


def _nullable_text(raw: Mapping[str, object], key: str) -> str | None:
    if key not in raw:
        raise ConfigurationError(f"{key} is required")
    return None if raw[key] is None else Text(raw, key)


__all__ = ["ParseCachePolicy", "ParsePerformanceBudgets"]
