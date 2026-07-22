from __future__ import annotations

import math
import os
from collections.abc import Mapping
from pathlib import Path

from trader.infrastructure.settings_credentials import CREDENTIAL_FILE_NAME, read_credential_file
from trader.infrastructure.settings_market_policy import (
    parse_cache_policy as _parse_cache_policy,
)
from trader.infrastructure.settings_market_policy import (
    parse_performance_budgets as _parse_performance_budgets,
)
from trader.infrastructure.settings_models import (
    ApiSettings,
    DeepSeekSettings,
    MarketDataSettings,
    PipelineSettings,
    RuntimeSettings,
    ServerSettings,
    TushareSettings,
)
from trader.infrastructure.settings_parser import (
    ConfigurationError,
)
from trader.infrastructure.settings_parser import (
    boolean as _boolean,
)
from trader.infrastructure.settings_parser import (
    environment_integer as _environment_integer,
)
from trader.infrastructure.settings_parser import (
    infer_project_root as _infer_project_root,
)
from trader.infrastructure.settings_parser import (
    integer as _integer,
)
from trader.infrastructure.settings_parser import (
    integer_mapping as _integer_mapping,
)
from trader.infrastructure.settings_parser import (
    mapping as _mapping,
)
from trader.infrastructure.settings_parser import (
    number as _number,
)
from trader.infrastructure.settings_parser import (
    read_json_object as _read_json_object,
)
from trader.infrastructure.settings_parser import (
    require_exact_keys as _require_exact_keys,
)
from trader.infrastructure.settings_parser import (
    resolve_config_path as _resolve_config_path,
)
from trader.infrastructure.settings_parser import (
    resolve_project_path as _resolve_project_path,
)
from trader.infrastructure.settings_parser import (
    text as _text,
)


def load_runtime_settings(config_path: str | os.PathLike[str]) -> RuntimeSettings:
    path = Path(config_path).expanduser().resolve()
    raw = _read_json_object(path)
    _require_exact_keys(
        raw,
        {
            "schema_version",
            "config_version",
            "runtime_dir",
            "strategy_config",
            "long_watchlist",
            "server",
            "pipeline",
            "market_data",
            "performance_budgets",
            "deepseek",
            "api",
        },
        "runtime",
    )
    if _integer(raw, "schema_version", minimum=1) != 5:
        raise ConfigurationError("runtime schema_version must be 5")

    config_dir = path.parent
    project_root = _infer_project_root(config_dir)
    server_raw = _mapping(raw, "server")
    pipeline_raw = _mapping(raw, "pipeline")
    market_raw = _mapping(raw, "market_data")
    performance_raw = _mapping(raw, "performance_budgets")
    deepseek_raw = _mapping(raw, "deepseek")
    api_raw = _mapping(raw, "api")
    _require_exact_keys(
        server_raw,
        {"host", "port", "debug", "use_reloader", "allow_insecure_non_loopback"},
        "server",
    )
    _require_exact_keys(
        pipeline_raw,
        {
            "event_queue_size",
            "priority_queue_size",
            "market_workers",
            "normalization_workers",
            "strategy_workers",
            "deepseek_workers",
            "shutdown_timeout_seconds",
            "cadence_seconds",
            "publish_heartbeat_seconds",
        },
        "pipeline",
    )
    _require_exact_keys(
        market_raw,
        {
            "eastmoney_timeout_seconds",
            "candidate_timeout_seconds",
            "history_timeout_seconds",
            "research_timeout_seconds",
            "minimum_market_rows",
            "candidate_pool_size",
            "single_flight",
            "circuit_breaker_failures",
            "circuit_breaker_seconds",
            "source_contract_versions",
            "tushare",
            "cache_policy",
        },
        "market_data",
    )
    _require_exact_keys(
        deepseek_raw,
        {
            "enabled",
            "base_url",
            "model",
            "challenger_model",
            "challenger_limits",
            "timeout_seconds",
            "batch_size",
            "max_tokens",
            "daily_hard_limit",
            "strategy_limits",
            "stage_targets",
            "stage_limits",
        },
        "deepseek",
    )
    _require_exact_keys(
        api_raw,
        {
            "default_top_n",
            "maximum_top_n",
            "event_page_limit",
            "maximum_event_page_limit",
            "sse_history_size",
            "sse_client_queue_size",
            "sse_max_clients",
        },
        "api",
    )

    host = os.environ.get("TRADER_HOST", _text(server_raw, "host"))
    port = _environment_integer("TRADER_PORT", _integer(server_raw, "port", minimum=1, maximum=65535))
    runtime_dir = _resolve_project_path(project_root, _text(raw, "runtime_dir"))
    strategy_config_path = _resolve_config_path(config_dir, _text(raw, "strategy_config"))
    long_watchlist_path = _resolve_config_path(config_dir, _text(raw, "long_watchlist"))

    settings = RuntimeSettings(
        schema_version=5,
        config_version=_text(raw, "config_version"),
        config_path=path,
        project_root=project_root,
        runtime_dir=runtime_dir,
        strategy_config_path=strategy_config_path,
        long_watchlist_path=long_watchlist_path,
        server=ServerSettings(
            host=host,
            port=port,
            debug=_boolean(server_raw, "debug"),
            use_reloader=_boolean(server_raw, "use_reloader"),
            allow_insecure_non_loopback=_boolean(server_raw, "allow_insecure_non_loopback"),
        ),
        pipeline=PipelineSettings(
            event_queue_size=_integer(pipeline_raw, "event_queue_size", minimum=1),
            priority_queue_size=_integer(pipeline_raw, "priority_queue_size", minimum=1),
            market_workers=_integer(pipeline_raw, "market_workers", minimum=1),
            normalization_workers=_integer(pipeline_raw, "normalization_workers", minimum=1),
            strategy_workers=_integer(pipeline_raw, "strategy_workers", minimum=1),
            deepseek_workers=_integer(pipeline_raw, "deepseek_workers", minimum=1),
            shutdown_timeout_seconds=_number(pipeline_raw, "shutdown_timeout_seconds", minimum=0.1),
            cadence_seconds=_nested_positive_number_mapping(pipeline_raw, "cadence_seconds"),
            publish_heartbeat_seconds=_integer(pipeline_raw, "publish_heartbeat_seconds", minimum=1),
        ),
        market_data=MarketDataSettings(
            eastmoney_timeout_seconds=_number(market_raw, "eastmoney_timeout_seconds", minimum=0.1),
            candidate_timeout_seconds=_number(market_raw, "candidate_timeout_seconds", minimum=0.1),
            history_timeout_seconds=_number(market_raw, "history_timeout_seconds", minimum=0.1),
            research_timeout_seconds=_number(
                market_raw,
                "research_timeout_seconds",
                minimum=0.1,
                maximum=8.0,
            ),
            minimum_market_rows=_integer(market_raw, "minimum_market_rows", minimum=1),
            candidate_pool_size=_integer(market_raw, "candidate_pool_size", minimum=1, maximum=1000),
            single_flight=_boolean(market_raw, "single_flight"),
            circuit_breaker_failures=_integer(market_raw, "circuit_breaker_failures", minimum=1),
            circuit_breaker_seconds=_integer(market_raw, "circuit_breaker_seconds", minimum=1),
            source_contract_versions=_text_mapping(market_raw, "source_contract_versions"),
            tushare=_parse_tushare_settings(_mapping(market_raw, "tushare"), project_root),
            cache_policy=_parse_cache_policy(_mapping(market_raw, "cache_policy")),
        ),
        performance_budgets=_parse_performance_budgets(performance_raw),
        deepseek=DeepSeekSettings(
            enabled=_boolean(deepseek_raw, "enabled"),
            base_url=_text(deepseek_raw, "base_url").rstrip("/"),
            model=_text(deepseek_raw, "model"),
            challenger_model=_text(deepseek_raw, "challenger_model"),
            challenger_limits=_integer_mapping(deepseek_raw, "challenger_limits", minimum=0),
            timeout_seconds=_number(deepseek_raw, "timeout_seconds", minimum=0.1),
            batch_size=_integer(deepseek_raw, "batch_size", minimum=1, maximum=8),
            max_tokens=_integer(deepseek_raw, "max_tokens", minimum=64),
            daily_hard_limit=_integer(deepseek_raw, "daily_hard_limit", minimum=0, maximum=188),
            strategy_limits=_integer_mapping(deepseek_raw, "strategy_limits", minimum=0),
            stage_targets=_integer_mapping(deepseek_raw, "stage_targets", minimum=0),
            stage_limits=_integer_mapping(deepseek_raw, "stage_limits", minimum=0),
            api_key=_load_deepseek_api_key(project_root),
        ),
        api=ApiSettings(
            default_top_n=_integer(api_raw, "default_top_n", minimum=0),
            maximum_top_n=_integer(api_raw, "maximum_top_n", minimum=0, maximum=18),
            event_page_limit=_integer(api_raw, "event_page_limit", minimum=1),
            maximum_event_page_limit=_integer(api_raw, "maximum_event_page_limit", minimum=1),
            sse_history_size=_integer(api_raw, "sse_history_size", minimum=1),
            sse_client_queue_size=_integer(api_raw, "sse_client_queue_size", minimum=1),
            sse_max_clients=_integer(api_raw, "sse_max_clients", minimum=1, maximum=256),
        ),
    )
    _validate_runtime_settings(settings)
    return settings


def _load_deepseek_api_key(project_root: Path) -> str:
    environment_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if environment_key:
        return environment_key
    configured_path = os.environ.get("DEEPSEEK_API_KEY_FILE", "").strip()
    key_path = Path(configured_path).expanduser() if configured_path else project_root / CREDENTIAL_FILE_NAME
    if not key_path.is_absolute():
        key_path = project_root / key_path
    key_path = key_path.resolve()
    if not key_path.exists():
        if configured_path:
            raise ConfigurationError("DEEPSEEK_API_KEY_FILE does not exist")
        return ""
    values = read_credential_file(key_path, label="DeepSeek API key", raw_key="DEEPSEEK_API_KEY")
    value = values.get("DEEPSEEK_API_KEY", "")
    if configured_path and not value:
        raise ConfigurationError("DEEPSEEK_API_KEY_FILE does not contain DEEPSEEK_API_KEY")
    return value


def _load_tushare_token(project_root: Path, configured_file: str) -> tuple[str, Path | None]:
    environment_token = os.environ.get("TUSHARE_TOKEN", "").strip()
    if environment_token:
        return environment_token, None
    environment_path = os.environ.get("TUSHARE_TOKEN_FILE", "").strip()
    selected_path = environment_path or configured_file.strip()
    if not selected_path:
        return "", None
    token_path = Path(selected_path).expanduser()
    if not token_path.is_absolute():
        token_path = project_root / token_path
    token_path = token_path.resolve()
    if not token_path.exists():
        if environment_path:
            raise ConfigurationError("TUSHARE_TOKEN_FILE does not exist")
        return "", token_path
    values = read_credential_file(token_path, label="Tushare token", raw_key="TUSHARE_TOKEN")
    value = values.get("TUSHARE_TOKEN", "")
    if not value:
        raise ConfigurationError("Tushare token file does not contain TUSHARE_TOKEN")
    return value, token_path


def _parse_tushare_settings(raw: Mapping[str, object], project_root: Path) -> TushareSettings:
    _require_exact_keys(raw, {"enabled", "points", "timeout_seconds", "token_file"}, "market_data.tushare")
    timeout = _number(raw, "timeout_seconds", minimum=0.1, maximum=8.0)
    if timeout != 8.0:
        raise ConfigurationError("market_data.tushare.timeout_seconds must be fixed at 8")
    token_file = _text(raw, "token_file")
    token, resolved_token_file = _load_tushare_token(project_root, token_file)
    return TushareSettings(
        enabled=_boolean(raw, "enabled"),
        points=_integer(raw, "points", minimum=120),
        timeout_seconds=timeout,
        token_file=resolved_token_file,
        token=token,
    )


def _text_mapping(raw: Mapping[str, object], key: str) -> dict[str, str]:
    values = _mapping(raw, key)
    result = {str(name): _text(values, str(name)) for name in values}
    expected = {"eastmoney", "sina", "tencent", "tushare", "akshare"}
    if set(result) != expected:
        raise ConfigurationError("market_data.source_contract_versions must define all five sources")
    return result


def _validate_runtime_settings(settings: RuntimeSettings) -> None:
    if settings.pipeline.priority_queue_size >= settings.pipeline.event_queue_size:
        raise ConfigurationError("priority_queue_size must be smaller than event_queue_size")
    if settings.api.default_top_n > settings.api.maximum_top_n:
        raise ConfigurationError("default_top_n cannot exceed maximum_top_n")
    if settings.api.event_page_limit > settings.api.maximum_event_page_limit:
        raise ConfigurationError("event_page_limit cannot exceed maximum_event_page_limit")
    if settings.pipeline.market_workers != 5:
        raise ConfigurationError("pipeline.market_workers must be fixed at 5 for the five source lanes")
    if not settings.market_data.single_flight:
        raise ConfigurationError("market_data.single_flight must remain enabled")
    if settings.market_data.cache_policy.total_bytes != settings.performance_budgets.memory.cache_total_bytes:
        raise ConfigurationError("cache and performance total byte budgets must match")
    if settings.market_data.candidate_pool_size != 120:
        raise ConfigurationError("market_data.candidate_pool_size must remain fixed at 120")
    if sum(settings.deepseek.strategy_limits.values()) != settings.deepseek.daily_hard_limit:
        raise ConfigurationError("DeepSeek strategy limits must sum to the daily hard limit")
    required_buckets = {"today", "tomorrow", "d25", "long", "shared_preheat", "emergency"}
    if set(settings.deepseek.strategy_limits) != required_buckets:
        raise ConfigurationError("DeepSeek strategy limits must define all six budget buckets")
    expected_strategy_limits = {
        "today": 70,
        "tomorrow": 45,
        "d25": 35,
        "long": 18,
        "shared_preheat": 15,
        "emergency": 5,
    }
    if dict(settings.deepseek.strategy_limits) != expected_strategy_limits:
        raise ConfigurationError("DeepSeek strategy limits must match the section 16 allocation")
    if settings.deepseek.base_url != "https://api.deepseek.com":
        raise ConfigurationError("DeepSeek base_url must use the official https://api.deepseek.com endpoint")
    if settings.deepseek.model != "deepseek-v4-flash":
        raise ConfigurationError("DeepSeek primary model must be deepseek-v4-flash")
    if settings.deepseek.challenger_model != "deepseek-v4-pro":
        raise ConfigurationError("DeepSeek challenger model must be deepseek-v4-pro")
    expected_challenger_limits = {"today": 6, "tomorrow": 6, "d25": 5, "long": 0}
    if dict(settings.deepseek.challenger_limits) != expected_challenger_limits:
        raise ConfigurationError("DeepSeek challenger limits must match the section 16 allocation")
    expected_stage_targets = {
        "shared_preheat": 15,
        "today_observe": 14,
        "today_main": 42,
        "today_late": 12,
        "tomorrow_afternoon": 21,
        "tomorrow_final": 14,
        "d25_afternoon": 19,
        "d25_final": 11,
        "long_afternoon": 10,
        "emergency": 0,
    }
    expected_stage_limits = {
        "shared_preheat": 15,
        "today_observe": 15,
        "today_main": 42,
        "today_late": 13,
        "tomorrow_afternoon": 25,
        "tomorrow_final": 20,
        "d25_afternoon": 22,
        "d25_final": 13,
        "long_afternoon": 18,
        "emergency": 5,
    }
    if dict(settings.deepseek.stage_targets) != expected_stage_targets:
        raise ConfigurationError("DeepSeek stage targets must match the section 16 allocation")
    if dict(settings.deepseek.stage_limits) != expected_stage_limits:
        raise ConfigurationError("DeepSeek stage limits must match the section 16 allocation")
    _validate_cadence_settings(settings.pipeline.cadence_seconds)


def _nested_positive_number_mapping(
    raw: Mapping[str, object],
    key: str,
) -> dict[str, Mapping[str, float]]:
    values = _mapping(raw, key)
    result: dict[str, Mapping[str, float]] = {}
    for task, task_raw in values.items():
        if not isinstance(task, str) or not isinstance(task_raw, dict) or not task_raw:
            raise ConfigurationError(f"{key} must contain non-empty task objects")
        intervals: dict[str, float] = {}
        for band, value in task_raw.items():
            if not isinstance(band, str) or not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ConfigurationError(f"{key}.{task} must contain numeric intervals")
            interval = float(value)
            if not math.isfinite(interval) or interval <= 0.0:
                raise ConfigurationError(f"{key}.{task}.{band} must be a positive finite interval")
            intervals[band] = interval
        result[task] = intervals
    return result


def _validate_cadence_settings(cadence: Mapping[str, Mapping[str, float]]) -> None:
    expected = {
        "full_market": {
            "warmup": 10.0,
            "today_main": 5.0,
            "today_late": 5.0,
            "midday": 10.0,
            "afternoon": 5.0,
            "final_review": 3.0,
        },
        "candidate_quotes": {
            "warmup": 2.0,
            "today_main": 1.0,
            "today_late": 2.0,
            "midday": 10.0,
            "afternoon": 2.0,
            "final_review": 1.0,
            "final_window": 1.0,
        },
        "topk_quotes": {
            "warmup": 1.0,
            "today_main": 1.0,
            "today_late": 1.0,
            "midday": 10.0,
            "afternoon": 1.0,
            "final_review": 1.0,
            "final_window": 1.0,
        },
        "score": {
            "warmup": 10.0,
            "today_main": 3.0,
            "today_late": 5.0,
            "afternoon": 5.0,
            "final_review": 3.0,
        },
        "industry_heat": {
            "warmup": 120.0,
            "today_main": 60.0,
            "today_late": 60.0,
            "afternoon": 60.0,
            "final_review": 60.0,
        },
        "market_news": {
            "warmup": 120.0,
            "today_main": 60.0,
            "today_late": 60.0,
            "afternoon": 60.0,
            "final_review": 60.0,
        },
        "stock_risk": {
            "warmup": 300.0,
            "today_main": 180.0,
            "today_late": 180.0,
            "afternoon": 180.0,
            "final_review": 120.0,
        },
    }
    if cadence != expected:
        raise ConfigurationError("pipeline cadence_seconds must match the fixed section 6 cadence table")
