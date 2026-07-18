"""Validated configuration loading at the application boundary."""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from trader.domain.factors import PRODUCTION_FACTOR_IDS
from trader.domain.news import NewsSignalPolicy
from trader.domain.research import D25SignalPolicy, LongResearchPolicy
from trader.domain.tail import TailSignalPolicy
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
    cadence_seconds: Mapping[str, Mapping[str, float]]
    publish_heartbeat_seconds: int


@dataclass(frozen=True)
class MarketDataSettings:
    eastmoney_timeout_seconds: float
    candidate_timeout_seconds: float
    history_timeout_seconds: float
    research_timeout_seconds: float
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
    daily_hard_limit: int
    strategy_limits: Mapping[str, int]
    stage_targets: Mapping[str, int]
    stage_limits: Mapping[str, int]
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
    veto: bool
    allowed_evidence_types: tuple[str, ...]
    strategies: tuple[str, ...]
    trigger_factor: str
    trigger_operator: str
    trigger_thresholds: tuple[float, ...]
    combination_mode: str
    risk_fact_id_fields: tuple[str, ...]


@dataclass(frozen=True)
class FactorDefinition:
    factor_id: str
    strategies: tuple[str, ...]
    raw_inputs: tuple[str, ...]
    formula: str
    unit: str
    direction: str
    observation_time: str
    adjustment: str
    lookback_window: int
    minimum_samples: int
    winsor_enabled: bool
    winsor_lower_quantile: float
    winsor_upper_quantile: float
    normalization: str
    missing_policy: str
    output_range: tuple[float, float]
    version: str


@dataclass(frozen=True)
class StrategySettings:
    schema_version: int
    strategy_version: str
    fusion: FusionSettings
    selection: SelectionSettings
    candidate_weights: Mapping[str, float]
    today_news_signal: NewsSignalPolicy
    tomorrow_tail_signal: TailSignalPolicy
    d25_signal: D25SignalPolicy
    long_research: LongResearchPolicy
    dimension_weights: Mapping[str, Mapping[str, float]]
    risk_rules: tuple[RiskRuleSettings, ...]
    factor_contract: Mapping[str, object]
    factor_registry: Mapping[str, FactorDefinition]


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
    if _integer(raw, "schema_version", minimum=1) != 3:
        raise ConfigurationError("runtime schema_version must be 3")

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
        schema_version=3,
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
        ),
        deepseek=DeepSeekSettings(
            enabled=_boolean(deepseek_raw, "enabled"),
            base_url=_text(deepseek_raw, "base_url").rstrip("/"),
            model=_text(deepseek_raw, "model"),
            timeout_seconds=_number(deepseek_raw, "timeout_seconds", minimum=0.1),
            batch_size=_integer(deepseek_raw, "batch_size", minimum=1, maximum=8),
            max_tokens=_integer(deepseek_raw, "max_tokens", minimum=64),
            daily_hard_limit=_integer(deepseek_raw, "daily_hard_limit", minimum=0, maximum=188),
            strategy_limits=_integer_mapping(deepseek_raw, "strategy_limits", minimum=0),
            stage_targets=_integer_mapping(deepseek_raw, "stage_targets", minimum=0),
            stage_limits=_integer_mapping(deepseek_raw, "stage_limits", minimum=0),
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
    if _integer(raw, "schema_version", minimum=1) != 7:
        raise ConfigurationError("strategy schema_version must be 7")
    fusion_raw = _mapping(raw, "fusion")
    selection_raw = _mapping(raw, "selection")
    rules_raw = raw.get("risk_rules")
    if not isinstance(rules_raw, list):
        raise ConfigurationError("risk_rules must be a list")
    risk_rules = tuple(_parse_risk_rule(item, index) for index, item in enumerate(rules_raw))
    dimension_weights = _nested_number_mapping(raw, "dimension_weights")
    today_news_signal = _parse_news_signal_policy(_mapping(raw, "today_news_signal"))
    tomorrow_tail_signal = _parse_tail_signal_policy(_mapping(raw, "tomorrow_tail_signal"))
    d25_signal = _parse_d25_signal_policy(_mapping(raw, "d25_signal"))
    long_research = _parse_long_research_policy(_mapping(raw, "long_research"))
    factor_registry_raw = _mapping(raw, "factor_registry")
    factor_registry = {
        str(factor_id): _parse_factor_definition(str(factor_id), definition)
        for factor_id, definition in factor_registry_raw.items()
    }
    settings = StrategySettings(
        schema_version=7,
        strategy_version=_strategy_contract_version(raw),
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
        today_news_signal=today_news_signal,
        tomorrow_tail_signal=tomorrow_tail_signal,
        d25_signal=d25_signal,
        long_research=long_research,
        dimension_weights=dimension_weights,
        risk_rules=risk_rules,
        factor_contract=dict(_mapping(raw, "factor_contract")),
        factor_registry=factor_registry,
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
    allowed_evidence_types = raw.get("allowed_evidence_types")
    if not isinstance(allowed_evidence_types, list) or any(
        not isinstance(value, str) or not value for value in allowed_evidence_types
    ):
        raise ConfigurationError(f"risk rule {index} allowed_evidence_types must be a list of non-empty strings")
    strategies = raw.get("strategies")
    if (
        not isinstance(strategies, list)
        or not strategies
        or any(not isinstance(value, str) or value not in {"today", "tomorrow", "d25", "long"} for value in strategies)
        or len(strategies) != len(set(strategies))
    ):
        raise ConfigurationError(f"risk rule {index} strategies must contain supported strategies")
    trigger = _mapping(raw, "trigger")
    operator = _text(trigger, "operator")
    thresholds = trigger.get("thresholds")
    if not isinstance(thresholds, list) or any(
        not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value))
        for value in thresholds
    ):
        raise ConfigurationError(f"risk rule {index} trigger thresholds must be finite numbers")
    expected_thresholds = {"gte": 1, "eq": 1, "gte_lt": 2}
    if operator not in expected_thresholds or len(thresholds) != expected_thresholds[operator]:
        raise ConfigurationError(f"risk rule {index} trigger operator or threshold count is invalid")
    if operator == "gte_lt" and not float(thresholds[0]) < float(thresholds[1]):
        raise ConfigurationError(f"risk rule {index} trigger range must be increasing")
    combination_mode = _text(raw, "combination_mode")
    if combination_mode not in {"additive", "exclusive"}:
        raise ConfigurationError(f"risk rule {index} combination_mode must be additive or exclusive")
    fact_id_fields = raw.get("risk_fact_id_fields")
    required_id_fields = {"stock_code", "risk_code", "actual", "source", "trade_date"}
    if (
        not isinstance(fact_id_fields, list)
        or any(not isinstance(value, str) or not value for value in fact_id_fields)
        or len(fact_id_fields) != len(set(fact_id_fields))
        or set(fact_id_fields) != required_id_fields
    ):
        raise ConfigurationError(f"risk rule {index} risk_fact_id_fields must define the stable identity fields")
    return RiskRuleSettings(
        risk_code=_text(raw, "risk_code"),
        severity=_text(raw, "severity"),
        penalty=_number(raw, "penalty", minimum=0.0, maximum=30.0),
        minimum_confidence=_number(raw, "minimum_confidence", minimum=0.0, maximum=1.0),
        evidence_ttl_hours=_integer(raw, "evidence_ttl_hours", minimum=1),
        group=_text(raw, "group"),
        veto=_boolean(raw, "veto"),
        allowed_evidence_types=tuple(allowed_evidence_types),
        strategies=tuple(strategies),
        trigger_factor=_text(trigger, "factor"),
        trigger_operator=operator,
        trigger_thresholds=tuple(float(value) for value in thresholds),
        combination_mode=combination_mode,
        risk_fact_id_fields=tuple(str(value) for value in fact_id_fields),
    )


def _parse_news_signal_policy(raw: Mapping[str, object]) -> NewsSignalPolicy:
    try:
        return NewsSignalPolicy(
            lookback_hours=_number(raw, "lookback_hours", minimum=0.01),
            freshness_full_score_hours=_number(raw, "freshness_full_score_hours", minimum=0.0),
            positive_score=_number(raw, "positive_score", minimum=0.0, maximum=100.0),
            neutral_score=_number(raw, "neutral_score", minimum=0.0, maximum=100.0),
            negative_score=_number(raw, "negative_score", minimum=0.0, maximum=100.0),
            positive_keywords=_keyword_tuple(raw, "positive_keywords"),
            negative_keywords=_keyword_tuple(raw, "negative_keywords"),
        )
    except ValueError as exc:
        raise ConfigurationError(f"today_news_signal {exc}") from exc


def _parse_tail_signal_policy(raw: Mapping[str, object]) -> TailSignalPolicy:
    try:
        return TailSignalPolicy(
            lookback_minutes=_integer(raw, "lookback_minutes", minimum=1),
            minimum_baseline_minutes=_integer(raw, "minimum_baseline_minutes", minimum=1),
            return_score_points_per_pct=_number(raw, "return_score_points_per_pct", minimum=0.01),
            volume_score_points_per_ratio=_number(raw, "volume_score_points_per_ratio", minimum=0.01),
        )
    except ValueError as exc:
        raise ConfigurationError(f"tomorrow_tail_signal {exc}") from exc


def _parse_d25_signal_policy(raw: Mapping[str, object]) -> D25SignalPolicy:
    overheat = _mapping(raw, "overheat")
    regime = _mapping(raw, "market_regime")
    try:
        return D25SignalPolicy(
            overheat_full_return_max=_number(overheat, "full_return_max"),
            overheat_linear_return_max=_number(overheat, "linear_return_max"),
            overheat_linear_end_factor=_number(overheat, "linear_end_factor", minimum=0.0, maximum=1.0),
            overheat_above_factor=_number(overheat, "above_factor", minimum=0.0, maximum=1.0),
            risk_on_breadth_min=_number(regime, "risk_on_breadth_min", minimum=0.0, maximum=100.0),
            risk_off_breadth_max=_number(regime, "risk_off_breadth_max", minimum=0.0, maximum=100.0),
            risk_on_factor=_number(regime, "risk_on_factor", minimum=0.01, maximum=2.0),
            neutral_factor=_number(regime, "neutral_factor", minimum=0.01, maximum=2.0),
            risk_off_factor=_number(regime, "risk_off_factor", minimum=0.01, maximum=2.0),
        )
    except ValueError as exc:
        raise ConfigurationError(f"d25_signal {exc}") from exc


def _parse_long_research_policy(raw: Mapping[str, object]) -> LongResearchPolicy:
    financial = _mapping(raw, "financial")
    announcements = _mapping(raw, "announcements")
    valuation = _mapping(raw, "valuation")
    growth = _mapping(raw, "growth")
    quality = _mapping(raw, "quality")
    deterioration = _mapping(raw, "financial_deterioration")
    unlock = _mapping(raw, "unlock")
    try:
        return LongResearchPolicy(
            financial_max_age_days=_integer(financial, "maximum_age_days", minimum=1),
            announcement_lookback_days=_integer(announcements, "lookback_days", minimum=1),
            announcement_limit=_integer(announcements, "maximum_rows", minimum=1, maximum=100),
            unlock_forward_days=_integer(unlock, "forward_days", minimum=1),
            pe_full_score_max=_number(valuation, "pe_full_score_max", minimum=0.01),
            pe_zero_score_min=_number(valuation, "pe_zero_score_min", minimum=0.01),
            pb_full_score_max=_number(valuation, "pb_full_score_max", minimum=0.01),
            pb_zero_score_min=_number(valuation, "pb_zero_score_min", minimum=0.01),
            growth_points_per_pct=_number(growth, "score_points_per_pct", minimum=0.01),
            quality_roe_neutral_pct=_number(quality, "roe_neutral_pct"),
            quality_roe_points_per_pct=_number(quality, "roe_score_points_per_pct", minimum=0.01),
            financial_revenue_deterioration_pct=_number(deterioration, "revenue_growth_lte"),
            financial_profit_deterioration_pct=_number(deterioration, "net_profit_growth_lte"),
            financial_core_profit_deterioration_pct=_number(deterioration, "core_profit_growth_lte"),
            pledge_thresholds=_threshold_tuple(raw, "pledge_thresholds", section="long_research"),
            unlock_thresholds=_threshold_tuple(unlock, "thresholds", section="long_research.unlock"),
            policy_keyword_score_step=_number(announcements, "policy_keyword_score_step", minimum=0.01),
            negative_high_keywords=_keyword_tuple(
                announcements, "negative_high_keywords", section="long_research.announcements"
            ),
            negative_medium_keywords=_keyword_tuple(
                announcements, "negative_medium_keywords", section="long_research.announcements"
            ),
            negative_low_keywords=_keyword_tuple(
                announcements, "negative_low_keywords", section="long_research.announcements"
            ),
            reduction_high_keywords=_keyword_tuple(
                announcements, "reduction_high_keywords", section="long_research.announcements"
            ),
            reduction_medium_keywords=_keyword_tuple(
                announcements, "reduction_medium_keywords", section="long_research.announcements"
            ),
            reduction_low_keywords=_keyword_tuple(
                announcements, "reduction_low_keywords", section="long_research.announcements"
            ),
            policy_positive_keywords=_keyword_tuple(
                announcements, "policy_positive_keywords", section="long_research.announcements"
            ),
            policy_negative_keywords=_keyword_tuple(
                announcements, "policy_negative_keywords", section="long_research.announcements"
            ),
        )
    except ValueError as exc:
        raise ConfigurationError(f"long_research {exc}") from exc


def _keyword_tuple(
    raw: Mapping[str, object],
    key: str,
    *,
    section: str = "today_news_signal",
) -> tuple[str, ...]:
    values = raw.get(key)
    if not isinstance(values, list) or not values or any(not isinstance(value, str) for value in values):
        raise ConfigurationError(f"{section}.{key} must be a non-empty string list")
    keywords = tuple(value.strip() for value in values)
    if any(not value or len(value) > 24 for value in keywords):
        raise ConfigurationError(f"{section}.{key} contains an invalid keyword")
    if len(keywords) > 100:
        raise ConfigurationError(f"{section}.{key} exceeds 100 keywords")
    return keywords


def _threshold_tuple(
    raw: Mapping[str, object],
    key: str,
    *,
    section: str,
) -> tuple[float, float, float]:
    values = raw.get(key)
    if (
        not isinstance(values, list)
        or len(values) != 3
        or any(not isinstance(value, (int, float)) or isinstance(value, bool) for value in values)
        or any(not math.isfinite(float(value)) for value in values)
    ):
        raise ConfigurationError(f"{section}.{key} must contain three finite numbers")
    return (float(values[0]), float(values[1]), float(values[2]))


def _validate_runtime_settings(settings: RuntimeSettings) -> None:
    if settings.pipeline.priority_queue_size >= settings.pipeline.event_queue_size:
        raise ConfigurationError("priority_queue_size must be smaller than event_queue_size")
    if settings.api.default_top_n > settings.api.maximum_top_n:
        raise ConfigurationError("default_top_n cannot exceed maximum_top_n")
    if settings.api.event_page_limit > settings.api.maximum_event_page_limit:
        raise ConfigurationError("event_page_limit cannot exceed maximum_event_page_limit")
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
    expected_stage_targets = {
        "shared_preheat": 15,
        "today_observe": 12,
        "today_main": 36,
        "today_late": 10,
        "tomorrow_afternoon": 15,
        "tomorrow_final": 10,
        "d25_afternoon": 15,
        "d25_final": 10,
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
            "warmup": 60.0,
            "today_main": 30.0,
            "today_late": 60.0,
            "midday": 60.0,
            "afternoon": 60.0,
            "final_review": 30.0,
        },
        "candidate_quotes": {
            "warmup": 10.0,
            "today_main": 5.0,
            "today_late": 10.0,
            "midday": 60.0,
            "afternoon": 10.0,
            "final_review": 3.0,
            "final_window": 2.0,
        },
        "topk_quotes": {
            "warmup": 5.0,
            "today_main": 3.0,
            "today_late": 5.0,
            "midday": 60.0,
            "afternoon": 5.0,
            "final_review": 2.0,
            "final_window": 3.0,
        },
        "score": {
            "warmup": 30.0,
            "today_main": 10.0,
            "today_late": 20.0,
            "afternoon": 30.0,
            "final_review": 10.0,
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
    if settings.fusion.local_risk_cap != 25.0 or settings.fusion.deepseek_risk_cap != 30.0:
        raise ConfigurationError("risk caps are fixed at 25 local and 30 DeepSeek")
    if settings.selection.default_top_k > settings.selection.maximum_top_k:
        raise ConfigurationError("default_top_k cannot exceed maximum_top_k")
    news = settings.today_news_signal
    if (
        news.lookback_hours != 72.0
        or news.freshness_full_score_hours != 1.0
        or news.positive_score != 75.0
        or news.neutral_score != 50.0
        or news.negative_score != 25.0
    ):
        raise ConfigurationError("today news signal window and scores are fixed at 72h/1h and 75/50/25")
    tail = settings.tomorrow_tail_signal
    if (
        tail.lookback_minutes != 30
        or tail.minimum_baseline_minutes != 30
        or tail.return_score_points_per_pct != 25.0
        or tail.volume_score_points_per_ratio != 50.0
    ):
        raise ConfigurationError("tomorrow tail signal formula is fixed at 30/30/25/50")
    d25 = settings.d25_signal
    if d25 != D25SignalPolicy(
        overheat_full_return_max=15.0,
        overheat_linear_return_max=30.0,
        overheat_linear_end_factor=0.85,
        overheat_above_factor=0.75,
        risk_on_breadth_min=60.0,
        risk_off_breadth_max=40.0,
        risk_on_factor=1.03,
        neutral_factor=1.0,
        risk_off_factor=0.92,
    ):
        raise ConfigurationError("d25 signal formula is fixed at 15/30/0.85/0.75 and 60/40/1.03/1/0.92")
    long = settings.long_research
    if (
        long.financial_max_age_days != 550
        or long.announcement_lookback_days != 180
        or long.announcement_limit != 100
        or long.unlock_forward_days != 90
        or long.pe_full_score_max != 10.0
        or long.pe_zero_score_min != 50.0
        or long.pb_full_score_max != 1.0
        or long.pb_zero_score_min != 8.0
        or long.growth_points_per_pct != 2.0
        or long.quality_roe_neutral_pct != 10.0
        or long.quality_roe_points_per_pct != 2.5
        or long.financial_revenue_deterioration_pct != -10.0
        or long.financial_profit_deterioration_pct != -20.0
        or long.financial_core_profit_deterioration_pct != -20.0
        or long.pledge_thresholds != (10.0, 20.0, 35.0)
        or long.unlock_thresholds != (1.0, 5.0, 10.0)
        or long.policy_keyword_score_step != 10.0
    ):
        raise ConfigurationError("long research windows, scoring slopes and risk thresholds are fixed")
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
    registered = set(settings.factor_registry)
    if registered != PRODUCTION_FACTOR_IDS:
        missing = sorted(PRODUCTION_FACTOR_IDS - registered)
        extra = sorted(registered - PRODUCTION_FACTOR_IDS)
        raise ConfigurationError(f"factor_registry mismatch: missing={missing}, extra={extra}")
    _validate_tomorrow_tail_factor_contract(settings)
    _validate_d25_factor_contract(settings)
    _validate_long_research_factor_contract(settings)
    required_risk_codes = {
        "near_limit_crowding",
        "price_volume_divergence",
        "high_volatility",
        "reduction_or_unlock_low",
        "reduction_or_unlock_medium",
        "reduction_or_unlock_high",
        "pledge_risk_low",
        "pledge_risk_medium",
        "pledge_risk_high",
        "financial_deterioration",
        "negative_announcement",
        "regulatory_risk",
    }
    if set(risk_codes) != required_risk_codes:
        raise ConfigurationError("risk_rules must define the complete local risk table")
    for rule in settings.risk_rules:
        definition = settings.factor_registry.get(rule.trigger_factor)
        if definition is None:
            raise ConfigurationError(f"risk rule {rule.risk_code} trigger factor is not registered")
        if not set(rule.strategies).issubset(definition.strategies):
            raise ConfigurationError(f"risk rule {rule.risk_code} uses a factor outside its registered strategies")
    group_modes: dict[str, str] = {}
    for rule in settings.risk_rules:
        existing = group_modes.setdefault(rule.group, rule.combination_mode)
        if existing != rule.combination_mode:
            raise ConfigurationError(f"risk group {rule.group} mixes combination modes")


def _validate_tomorrow_tail_factor_contract(settings: StrategySettings) -> None:
    _validate_tail_factor_definition(
        settings.factor_registry["tail_return_30m_pct"],
        raw_inputs=("unadjusted_completed_minute_close",),
        formula="(latest_close/close_30_continuous_trading_minutes_ago-1)*100",
        unit="percentage_points",
        minimum_samples=31,
        normalization="none",
        missing_policy="missing_and_record",
        output_range=(-100.0, 1000.0),
    )
    _validate_tail_factor_definition(
        settings.factor_registry["tail_return_30m"],
        raw_inputs=("tail_return_30m_pct",),
        formula="clamp(50+tail_return_30m_pct*25)",
        unit="score_0_100",
        minimum_samples=31,
        normalization="formula_0_100",
        missing_policy="neutral_50_and_record",
        output_range=(0.0, 100.0),
    )
    _validate_tail_factor_definition(
        settings.factor_registry["tail_volume_ratio_raw"],
        raw_inputs=("unadjusted_completed_minute_volume",),
        formula="mean(last_30_continuous_trading_minute_volume)/mean(valid_same_day_pre_tail_volume)",
        unit="ratio",
        minimum_samples=60,
        normalization="none",
        missing_policy="missing_and_record",
        output_range=(0.0, 1_000_000.0),
    )
    _validate_tail_factor_definition(
        settings.factor_registry["tail_volume_ratio"],
        raw_inputs=("tail_volume_ratio_raw",),
        formula="clamp(50+(tail_volume_ratio_raw-1)*50)",
        unit="score_0_100",
        minimum_samples=60,
        normalization="formula_0_100",
        missing_policy="neutral_50_and_record",
        output_range=(0.0, 100.0),
    )


def _validate_d25_factor_contract(settings: StrategySettings) -> None:
    _validate_factor_definition(
        settings.factor_registry["d25_overheat_factor"],
        {
            "strategies": ("d25",),
            "raw_inputs": ("return_20d",),
            "formula": "1 if return_20d<=15; 1-(return_20d-15)*0.15/15 if return_20d<=30; 0.75 if return_20d>30; missing=>1",
            "unit": "multiplier",
            "direction": "higher_better",
            "observation_time": "point_in_time",
            "adjustment": "forward",
            "lookback_window": 20,
            "minimum_samples": 21,
            "winsor_enabled": False,
            "normalization": "configured_piecewise",
            "missing_policy": "neutral_1_and_record",
            "output_range": (0.75, 1.0),
        },
    )
    _validate_factor_definition(
        settings.factor_registry["market_regime_factor"],
        {
            "strategies": ("d25",),
            "raw_inputs": ("market_breadth",),
            "formula": "1.03 if market_breadth>=60; 0.92 if market_breadth<=40; else 1.0",
            "unit": "multiplier",
            "direction": "higher_better",
            "observation_time": "same_data_version_cross_section",
            "adjustment": "none",
            "lookback_window": 0,
            "minimum_samples": 1,
            "winsor_enabled": False,
            "normalization": "configured_regime",
            "missing_policy": "neutral_1_and_record",
            "output_range": (0.92, 1.03),
        },
    )
    _validate_factor_definition(
        settings.factor_registry["return_20d_not_overheated"],
        {
            "raw_inputs": ("return_20d",),
            "formula": "100 if return<=15; 0 if return>=30; linear between",
            "lookback_window": 20,
            "minimum_samples": 21,
            "winsor_enabled": False,
            "normalization": "formula_0_100",
            "missing_policy": "neutral_50_and_record",
            "output_range": (0.0, 100.0),
        },
    )


def _validate_long_research_factor_contract(settings: StrategySettings) -> None:
    score_common = {
        "strategies": ("long",),
        "unit": "score_0_100",
        "direction": "higher_better",
        "winsor_enabled": False,
        "output_range": (0.0, 100.0),
    }
    risk_common = {
        "strategies": ("today", "tomorrow", "d25", "long"),
        "direction": "higher_worse",
        "adjustment": "none",
        "winsor_enabled": False,
    }
    expected = {
        "value_score": {
            **score_common,
            "raw_inputs": ("unadjusted_price", "point_in_time_financial_report"),
            "formula": "mean_known(inverse_linear(price/(EPSJB*annualizer),10,50),inverse_linear(price/BPS,1,8))",
            "observation_time": "latest_published_before_observation",
            "adjustment": "mixed_anchor_unadjusted_financial_point_in_time",
            "lookback_window": 550,
            "minimum_samples": 1,
            "normalization": "configured_formula_0_100",
            "missing_policy": "neutral_50_and_record",
        },
        "growth_score": {
            **score_common,
            "raw_inputs": ("point_in_time_financial_report",),
            "formula": "clamp(50+2*mean_known(TOTALOPERATEREVETZ,PARENTNETPROFITTZ,KCFJCXSYJLRTZ))",
            "observation_time": "latest_published_before_observation",
            "adjustment": "none",
            "lookback_window": 550,
            "minimum_samples": 1,
            "normalization": "configured_formula_0_100",
            "missing_policy": "neutral_50_and_record",
        },
        "quality_score": {
            **score_common,
            "raw_inputs": ("point_in_time_financial_report",),
            "formula": "mean_known(clamp(50+(ROEJQ*annualizer-10)*2.5),clamp(KCFJCXSYJLR/PARENTNETPROFIT*100))",
            "observation_time": "latest_published_before_observation",
            "adjustment": "none",
            "lookback_window": 550,
            "minimum_samples": 1,
            "normalization": "configured_formula_0_100",
            "missing_policy": "neutral_50_and_record",
        },
        "industry_policy_score": {
            **score_common,
            "raw_inputs": ("industry_strength", "validated_announcements"),
            "formula": "0.6*industry_strength+0.4*clamp(50+10*(unique_positive_keyword_hits-unique_negative_keyword_hits))",
            "observation_time": "latest_published_before_observation",
            "adjustment": "none",
            "lookback_window": 180,
            "minimum_samples": 1,
            "normalization": "configured_formula_0_100",
            "missing_policy": "neutral_50_and_record",
        },
        "risk_protection_score": {
            **score_common,
            "raw_inputs": ("low_volatility_score", "low_drawdown_score"),
            "formula": "0.5*low_volatility_score+0.5*low_drawdown_score",
            "observation_time": "point_in_time",
            "adjustment": "forward",
            "lookback_window": 20,
            "minimum_samples": 20,
            "normalization": "formula_0_100",
            "missing_policy": "neutral_50_and_record",
        },
        "financial_deterioration": {
            **risk_common,
            "raw_inputs": ("point_in_time_financial_report",),
            "formula": "1 if revenue_yoy<=-10 or net_profit_yoy<=-20 or core_profit_yoy<=-20 else 0",
            "unit": "risk_indicator",
            "observation_time": "latest_published_before_observation",
            "lookback_window": 550,
            "minimum_samples": 1,
            "normalization": "none",
            "missing_policy": "missing_and_record",
            "output_range": (0.0, 1.0),
        },
        "negative_announcement_level": {
            **risk_common,
            "raw_inputs": ("validated_announcements",),
            "formula": "max configured keyword severity over valid 180d announcements; empty_success=0",
            "unit": "severity_level",
            "observation_time": "latest_published_before_observation",
            "lookback_window": 180,
            "minimum_samples": 0,
            "normalization": "configured_severity_0_3",
            "missing_policy": "missing_and_record",
            "output_range": (0.0, 3.0),
        },
        "pledge_risk": {
            **risk_common,
            "raw_inputs": ("point_in_time_ACCUM_PLEDGE_TSR",),
            "formula": "severity(ACCUM_PLEDGE_TSR,[10,20,35]); empty_success=0",
            "unit": "severity_level",
            "observation_time": "point_in_time",
            "lookback_window": 0,
            "minimum_samples": 0,
            "normalization": "configured_severity_0_3",
            "missing_policy": "missing_and_record",
            "output_range": (0.0, 3.0),
        },
        "reduction_or_unlock": {
            **risk_common,
            "raw_inputs": ("validated_reduction_announcements", "upcoming_90d_TOTAL_RATIO"),
            "formula": "max(configured reduction announcement severity,severity(sum(upcoming_90d_TOTAL_RATIO*100),[1,5,10]))",
            "unit": "severity_level",
            "observation_time": "latest_published_before_observation",
            "lookback_window": 180,
            "minimum_samples": 0,
            "normalization": "configured_severity_0_3",
            "missing_policy": "missing_and_record",
            "output_range": (0.0, 3.0),
        },
    }
    for factor_id, factor_expected in expected.items():
        _validate_factor_definition(settings.factor_registry[factor_id], factor_expected)


def _validate_factor_definition(
    definition: FactorDefinition,
    expected: Mapping[str, object],
) -> None:
    for attribute, expected_value in expected.items():
        actual = getattr(definition, attribute)
        if attribute == "formula":
            actual = "".join(str(actual).split())
            expected_value = "".join(str(expected_value).split())
        if actual != expected_value:
            raise ConfigurationError(
                f"factor_registry.{definition.factor_id}.{attribute} contradicts the executable formula"
            )


def _validate_tail_factor_definition(
    definition: FactorDefinition,
    *,
    raw_inputs: tuple[str, ...],
    formula: str,
    unit: str,
    minimum_samples: int,
    normalization: str,
    missing_policy: str,
    output_range: tuple[float, float],
) -> None:
    expected: tuple[tuple[str, object], ...] = (
        ("strategies", ("tomorrow",)),
        ("raw_inputs", raw_inputs),
        ("formula", formula),
        ("unit", unit),
        ("direction", "higher_better"),
        ("observation_time", "latest_completed_minute_at_or_before_observation"),
        ("adjustment", "none"),
        ("lookback_window", 30),
        ("minimum_samples", minimum_samples),
        ("winsor_enabled", False),
        ("winsor_lower_quantile", 0.025),
        ("winsor_upper_quantile", 0.975),
        ("normalization", normalization),
        ("missing_policy", missing_policy),
        ("output_range", output_range),
    )
    for attribute, expected_value in expected:
        actual = getattr(definition, attribute)
        if attribute == "formula":
            actual = "".join(str(actual).split())
            expected_value = "".join(str(expected_value).split())
        if actual != expected_value:
            raise ConfigurationError(
                f"factor_registry.{definition.factor_id}.{attribute} contradicts the executable tomorrow tail formula"
            )


def _parse_factor_definition(factor_id: str, raw: object) -> FactorDefinition:
    if not isinstance(raw, dict):
        raise ConfigurationError(f"factor_registry.{factor_id} must be an object")
    if _text(raw, "factor_id") != factor_id:
        raise ConfigurationError(f"factor_registry.{factor_id}.factor_id must match its key")
    strategies = raw.get("strategies")
    raw_inputs = raw.get("raw_inputs")
    output_range = raw.get("output_range")
    winsor = _mapping(raw, "winsorization")
    if (
        not isinstance(strategies, list)
        or not strategies
        or any(value not in {"today", "tomorrow", "d25", "long"} for value in strategies)
    ):
        raise ConfigurationError(f"factor_registry.{factor_id}.strategies is invalid")
    if (
        not isinstance(raw_inputs, list)
        or not raw_inputs
        or any(not isinstance(value, str) or not value for value in raw_inputs)
    ):
        raise ConfigurationError(f"factor_registry.{factor_id}.raw_inputs is invalid")
    if (
        not isinstance(output_range, list)
        or len(output_range) != 2
        or any(not isinstance(value, (int, float)) or isinstance(value, bool) for value in output_range)
        or any(not math.isfinite(float(value)) for value in output_range)
        or float(output_range[0]) > float(output_range[1])
    ):
        raise ConfigurationError(f"factor_registry.{factor_id}.output_range is invalid")
    lower = _number(winsor, "lower_quantile", minimum=0.0, maximum=1.0)
    upper = _number(winsor, "upper_quantile", minimum=0.0, maximum=1.0)
    if lower > upper:
        raise ConfigurationError(f"factor_registry.{factor_id}.winsorization is invalid")
    return FactorDefinition(
        factor_id=factor_id,
        strategies=tuple(strategies),
        raw_inputs=tuple(raw_inputs),
        formula=_text(raw, "formula"),
        unit=_text(raw, "unit"),
        direction=_text(raw, "direction"),
        observation_time=_text(raw, "observation_time"),
        adjustment=_text(raw, "adjustment"),
        lookback_window=_integer(raw, "lookback_window", minimum=0),
        minimum_samples=_integer(raw, "minimum_samples", minimum=0),
        winsor_enabled=_boolean(winsor, "enabled"),
        winsor_lower_quantile=lower,
        winsor_upper_quantile=upper,
        normalization=_text(raw, "normalization"),
        missing_policy=_text(raw, "missing_policy"),
        output_range=(float(output_range[0]), float(output_range[1])),
        version=_text(raw, "version"),
    )


def _strategy_contract_version(raw: Mapping[str, object]) -> str:
    canonical = dict(raw)
    canonical.pop("strategy_version", None)
    try:
        payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except ValueError as exc:
        raise ConfigurationError("strategy configuration numbers must be finite") from exc
    return f"strategy_sha256_{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:20]}"


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
    "FactorDefinition",
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
