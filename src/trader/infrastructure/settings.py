"""Validated configuration loading at the application boundary."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

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
    nested_number_mapping as _nested_number_mapping,
)
from trader.infrastructure.settings_parser import (
    number as _number,
)
from trader.infrastructure.settings_parser import (
    number_mapping as _number_mapping,
)
from trader.infrastructure.settings_parser import (
    read_json_object as _read_json_object,
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


@dataclass(frozen=True)
class ServerSettings:
    host: str
    port: int
    debug: bool
    use_reloader: bool
    allow_insecure_non_loopback: bool


@dataclass(frozen=True)
class PipelineSettings:
    event_queue_size: int
    priority_queue_size: int
    market_workers: int
    normalization_workers: int
    strategy_workers: int
    deepseek_workers: int
    shutdown_timeout_seconds: float
    full_market_refresh_seconds: int
    candidate_refresh_seconds: int
    publish_heartbeat_seconds: int


@dataclass(frozen=True)
class MarketDataSettings:
    eastmoney_timeout_seconds: float
    candidate_timeout_seconds: float
    history_timeout_seconds: float
    minimum_market_rows: int
    candidate_pool_size: int
    single_flight: bool
    circuit_breaker_failures: int
    circuit_breaker_seconds: int


@dataclass(frozen=True)
class DeepSeekSettings:
    enabled: bool
    base_url: str
    model: str
    timeout_seconds: float
    batch_size: int
    max_tokens: int
    confidence_coverage_min: float
    daily_hard_limit: int
    strategy_limits: Mapping[str, int]
    api_key: str = field(default="", repr=False)


@dataclass(frozen=True)
class ApiSettings:
    default_top_n: int
    maximum_top_n: int
    event_page_limit: int
    maximum_event_page_limit: int
    sse_history_size: int
    sse_client_queue_size: int
    sse_max_clients: int


@dataclass(frozen=True)
class RuntimeSettings:
    schema_version: int
    config_version: str
    config_path: Path
    project_root: Path
    runtime_dir: Path
    strategy_config_path: Path
    long_watchlist_path: Path
    server: ServerSettings
    pipeline: PipelineSettings
    market_data: MarketDataSettings
    deepseek: DeepSeekSettings
    api: ApiSettings


@dataclass(frozen=True)
class FusionSettings:
    version: str
    local_weight: float
    deepseek_weight: float
    confidence_coverage_min: float
    minimum_known_dimensions: int
    local_risk_cap: float
    deepseek_risk_cap: float
    score_decimals: int
    rounding: str


@dataclass(frozen=True)
class SelectionSettings:
    default_top_k: int
    maximum_top_k: int
    maximum_per_industry: int
    observation_margin: float
    thresholds: Mapping[str, float]


@dataclass(frozen=True)
class RiskRuleSettings:
    risk_code: str
    severity: str
    penalty: float
    minimum_confidence: float
    evidence_ttl_hours: int
    group: str


@dataclass(frozen=True)
class StrategySettings:
    schema_version: int
    strategy_version: str
    fusion: FusionSettings
    selection: SelectionSettings
    candidate_weights: Mapping[str, float]
    dimension_weights: Mapping[str, Mapping[str, float]]
    risk_rules: tuple[RiskRuleSettings, ...]
    factor_contract: Mapping[str, object]


@dataclass(frozen=True)
class LongWatchItem:
    code: str
    name: str
    industry: str
    target_price: float | None


@dataclass(frozen=True)
class LongWatchlist:
    schema_version: int
    watchlist_version: str
    items: tuple[LongWatchItem, ...]


def load_runtime_settings(config_path: str | os.PathLike[str]) -> RuntimeSettings:
    path = Path(config_path).expanduser().resolve()
    raw = _read_json_object(path)
    if _integer(raw, "schema_version", minimum=1) != 2:
        raise ConfigurationError("runtime schema_version must be 2")

    config_dir = path.parent
    project_root = _infer_project_root(config_dir)
    server_raw = _mapping(raw, "server")
    pipeline_raw = _mapping(raw, "pipeline")
    market_raw = _mapping(raw, "market_data")
    deepseek_raw = _mapping(raw, "deepseek")
    api_raw = _mapping(raw, "api")

    host = os.environ.get("TRADER_HOST", _text(server_raw, "host"))
    port = _environment_integer("TRADER_PORT", _integer(server_raw, "port", minimum=1, maximum=65535))
    runtime_dir = _resolve_project_path(project_root, _text(raw, "runtime_dir"))
    strategy_config_path = _resolve_config_path(config_dir, _text(raw, "strategy_config"))
    long_watchlist_path = _resolve_config_path(config_dir, _text(raw, "long_watchlist"))

    settings = RuntimeSettings(
        schema_version=2,
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
            full_market_refresh_seconds=_integer(pipeline_raw, "full_market_refresh_seconds", minimum=1),
            candidate_refresh_seconds=_integer(pipeline_raw, "candidate_refresh_seconds", minimum=1),
            publish_heartbeat_seconds=_integer(pipeline_raw, "publish_heartbeat_seconds", minimum=1),
        ),
        market_data=MarketDataSettings(
            eastmoney_timeout_seconds=_number(market_raw, "eastmoney_timeout_seconds", minimum=0.1),
            candidate_timeout_seconds=_number(market_raw, "candidate_timeout_seconds", minimum=0.1),
            history_timeout_seconds=_number(market_raw, "history_timeout_seconds", minimum=0.1),
            minimum_market_rows=_integer(market_raw, "minimum_market_rows", minimum=1),
            candidate_pool_size=_integer(market_raw, "candidate_pool_size", minimum=1, maximum=1000),
            single_flight=_boolean(market_raw, "single_flight"),
            circuit_breaker_failures=_integer(market_raw, "circuit_breaker_failures", minimum=1),
            circuit_breaker_seconds=_integer(market_raw, "circuit_breaker_seconds", minimum=1),
        ),
        deepseek=DeepSeekSettings(
            enabled=_boolean(deepseek_raw, "enabled"),
            base_url=_text(deepseek_raw, "base_url").rstrip("/"),
            model=_text(deepseek_raw, "model"),
            timeout_seconds=_number(deepseek_raw, "timeout_seconds", minimum=0.1),
            batch_size=_integer(deepseek_raw, "batch_size", minimum=1, maximum=8),
            max_tokens=_integer(deepseek_raw, "max_tokens", minimum=64),
            confidence_coverage_min=_number(
                deepseek_raw,
                "confidence_coverage_min",
                minimum=0.0,
                maximum=1.0,
            ),
            daily_hard_limit=_integer(deepseek_raw, "daily_hard_limit", minimum=0, maximum=188),
            strategy_limits=_integer_mapping(deepseek_raw, "strategy_limits", minimum=0),
            api_key=os.environ.get("DEEPSEEK_API_KEY", "").strip(),
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


def load_strategy_settings(config_path: str | os.PathLike[str]) -> StrategySettings:
    path = Path(config_path).expanduser().resolve()
    raw = _read_json_object(path)
    if _integer(raw, "schema_version", minimum=1) != 2:
        raise ConfigurationError("strategy schema_version must be 2")
    fusion_raw = _mapping(raw, "fusion")
    selection_raw = _mapping(raw, "selection")
    rules_raw = raw.get("risk_rules")
    if not isinstance(rules_raw, list):
        raise ConfigurationError("risk_rules must be a list")
    risk_rules = tuple(_parse_risk_rule(item, index) for index, item in enumerate(rules_raw))
    dimension_weights = _nested_number_mapping(raw, "dimension_weights")
    settings = StrategySettings(
        schema_version=2,
        strategy_version=_text(raw, "strategy_version"),
        fusion=FusionSettings(
            version=_text(fusion_raw, "version"),
            local_weight=_number(fusion_raw, "local_weight", minimum=0.0, maximum=1.0),
            deepseek_weight=_number(fusion_raw, "deepseek_weight", minimum=0.0, maximum=1.0),
            confidence_coverage_min=_number(
                fusion_raw,
                "confidence_coverage_min",
                minimum=0.0,
                maximum=1.0,
            ),
            minimum_known_dimensions=_integer(fusion_raw, "minimum_known_dimensions", minimum=1, maximum=5),
            local_risk_cap=_number(fusion_raw, "local_risk_cap", minimum=0.0, maximum=100.0),
            deepseek_risk_cap=_number(fusion_raw, "deepseek_risk_cap", minimum=0.0, maximum=100.0),
            score_decimals=_integer(fusion_raw, "score_decimals", minimum=0, maximum=6),
            rounding=_text(fusion_raw, "rounding"),
        ),
        selection=SelectionSettings(
            default_top_k=_integer(selection_raw, "default_top_k", minimum=0, maximum=18),
            maximum_top_k=_integer(selection_raw, "maximum_top_k", minimum=0, maximum=18),
            maximum_per_industry=_integer(selection_raw, "maximum_per_industry", minimum=1),
            observation_margin=_number(selection_raw, "observation_margin", minimum=0.0),
            thresholds=_number_mapping(selection_raw, "thresholds"),
        ),
        candidate_weights=_number_mapping(raw, "candidate_weights"),
        dimension_weights=dimension_weights,
        risk_rules=risk_rules,
        factor_contract=dict(_mapping(raw, "factor_contract")),
    )
    _validate_strategy_settings(settings)
    return settings


def load_long_watchlist(config_path: str | os.PathLike[str]) -> LongWatchlist:
    raw = _read_json_object(Path(config_path).expanduser().resolve())
    items_raw = raw.get("items")
    if not isinstance(items_raw, list):
        raise ConfigurationError("long watchlist items must be a list")
    items: list[LongWatchItem] = []
    codes: set[str] = set()
    for index, item in enumerate(items_raw):
        if not isinstance(item, dict):
            raise ConfigurationError(f"long watchlist item {index} must be an object")
        code = _text(item, "code")
        if len(code) != 6 or not code.isdigit() or code in codes:
            raise ConfigurationError(f"invalid or duplicate long watchlist code: {code}")
        codes.add(code)
        target = item.get("target_price")
        if target is not None and (
            not isinstance(target, (int, float))
            or isinstance(target, bool)
            or not math.isfinite(float(target))
            or target <= 0
        ):
            raise ConfigurationError(f"invalid target_price for {code}")
        items.append(
            LongWatchItem(
                code=code,
                name=_text(item, "name"),
                industry=_text(item, "industry"),
                target_price=float(target) if target is not None else None,
            )
        )
    return LongWatchlist(
        schema_version=_integer(raw, "schema_version", minimum=1),
        watchlist_version=_text(raw, "watchlist_version"),
        items=tuple(items),
    )


def _parse_risk_rule(raw: object, index: int) -> RiskRuleSettings:
    if not isinstance(raw, dict):
        raise ConfigurationError(f"risk rule {index} must be an object")
    return RiskRuleSettings(
        risk_code=_text(raw, "risk_code"),
        severity=_text(raw, "severity"),
        penalty=_number(raw, "penalty", minimum=0.0, maximum=30.0),
        minimum_confidence=_number(raw, "minimum_confidence", minimum=0.0, maximum=1.0),
        evidence_ttl_hours=_integer(raw, "evidence_ttl_hours", minimum=1),
        group=_text(raw, "group"),
    )


def _validate_runtime_settings(settings: RuntimeSettings) -> None:
    if settings.api.default_top_n > settings.api.maximum_top_n:
        raise ConfigurationError("default_top_n cannot exceed maximum_top_n")
    if settings.api.event_page_limit > settings.api.maximum_event_page_limit:
        raise ConfigurationError("event_page_limit cannot exceed maximum_event_page_limit")
    if sum(settings.deepseek.strategy_limits.values()) != settings.deepseek.daily_hard_limit:
        raise ConfigurationError("DeepSeek strategy limits must sum to the daily hard limit")
    required_buckets = {"today", "tomorrow", "d25", "long", "shared_preheat", "emergency"}
    if set(settings.deepseek.strategy_limits) != required_buckets:
        raise ConfigurationError("DeepSeek strategy limits must define all six budget buckets")


def _validate_strategy_settings(settings: StrategySettings) -> None:
    if abs(settings.fusion.local_weight + settings.fusion.deepseek_weight - 1.0) > 1e-9:
        raise ConfigurationError("fusion weights must sum to 1.0")
    if abs(settings.fusion.local_weight - 0.68) > 1e-9 or abs(settings.fusion.deepseek_weight - 0.32) > 1e-9:
        raise ConfigurationError("fusion weights are fixed at 0.68 and 0.32")
    if settings.fusion.version != "fusion_v2_local68_deepseek32":
        raise ConfigurationError("unsupported fusion version")
    if settings.fusion.score_decimals != 2:
        raise ConfigurationError("fusion score_decimals must be 2")
    if settings.fusion.rounding != "ROUND_HALF_UP":
        raise ConfigurationError("unsupported score rounding mode")
    if settings.selection.default_top_k > settings.selection.maximum_top_k:
        raise ConfigurationError("default_top_k cannot exceed maximum_top_k")
    _validate_weight_sum("candidate_weights", settings.candidate_weights)
    required_candidate_weights = {
        "liquidity",
        "short_momentum",
        "trend",
        "industry_strength",
        "data_completeness",
    }
    if set(settings.candidate_weights) != required_candidate_weights:
        raise ConfigurationError("candidate_weights contains unsupported components")
    required_thresholds = {"today_main", "today_late", "tomorrow", "d25"}
    if set(settings.selection.thresholds) != required_thresholds:
        raise ConfigurationError("selection thresholds must define today_main, today_late, tomorrow and d25")
    required_strategies = {"today", "tomorrow", "d25", "long"}
    if set(settings.dimension_weights) != required_strategies:
        raise ConfigurationError("dimension_weights must define today, tomorrow, d25 and long")
    for strategy, weights in settings.dimension_weights.items():
        _validate_weight_sum(f"dimension_weights.{strategy}", weights)
        if set(weights) != {
            "value_quality",
            "financial_health",
            "market_flow",
            "industry_policy",
            "risk_quality",
        }:
            raise ConfigurationError(f"dimension_weights.{strategy} must define the five review dimensions")
    risk_codes = [rule.risk_code for rule in settings.risk_rules]
    if len(risk_codes) != len(set(risk_codes)):
        raise ConfigurationError("risk rule codes must be unique")
    if any(rule.severity not in {"low", "medium", "high"} for rule in settings.risk_rules):
        raise ConfigurationError("risk rule severity must be low, medium or high")


def _validate_weight_sum(name: str, weights: Mapping[str, float]) -> None:
    if not weights or abs(sum(weights.values()) - 1.0) > 1e-9:
        raise ConfigurationError(f"{name} must sum to 1.0")
    if any(weight < 0.0 or weight > 1.0 for weight in weights.values()):
        raise ConfigurationError(f"{name} weights must be between 0 and 1")


__all__ = [
    "ApiSettings",
    "ConfigurationError",
    "DeepSeekSettings",
    "FusionSettings",
    "LongWatchItem",
    "LongWatchlist",
    "MarketDataSettings",
    "PipelineSettings",
    "RiskRuleSettings",
    "RuntimeSettings",
    "SelectionSettings",
    "ServerSettings",
    "StrategySettings",
    "load_long_watchlist",
    "load_runtime_settings",
    "load_strategy_settings",
]
