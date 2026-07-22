"""Validated configuration loading at the application boundary."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from pathlib import Path

from trader.domain.news import NewsSignalPolicy
from trader.domain.research import D25SignalPolicy, LongResearchPolicy
from trader.domain.tail import TailSignalPolicy
from trader.infra.settings_factor_validation import _parse_factor_definition, _strategy_contract_version
from trader.infra.settings_models import (
    ApiSettings,
    DeepSeekSettings,
    FactorDefinition,
    FusionSettings,
    HardFilterSettings,
    LongWatchItem,
    LongWatchlist,
    MarketDataSettings,
    PipelineSettings,
    RiskRuleSettings,
    RuntimeSettings,
    SelectionSettings,
    ServerSettings,
    StrategySettings,
)
from trader.infra.settings_parser import (
    ConfigurationError,
)
from trader.infra.settings_parser import (
    boolean as _boolean,
)
from trader.infra.settings_parser import (
    integer as _integer,
)
from trader.infra.settings_parser import (
    mapping as _mapping,
)
from trader.infra.settings_parser import (
    nested_number_mapping as _nested_number_mapping,
)
from trader.infra.settings_parser import (
    number as _number,
)
from trader.infra.settings_parser import (
    number_mapping as _number_mapping,
)
from trader.infra.settings_parser import (
    read_json_object as _read_json_object,
)
from trader.infra.settings_parser import (
    text as _text,
)
from trader.infra.settings_parser import triple_nested_number_mapping as _triple_nested_number_mapping
from trader.infra.settings_runtime import load_runtime_settings
from trader.infra.settings_strategy_validation import _validate_strategy_settings


def load_strategy_settings(config_path: str | os.PathLike[str]) -> StrategySettings:
    path = Path(config_path).expanduser().resolve()
    raw = _read_json_object(path)
    if _integer(raw, "schema_version", minimum=1) != 10:
        raise ConfigurationError("strategy schema_version must be 10")
    fusion_raw = _mapping(raw, "fusion")
    selection_raw = _mapping(raw, "selection")
    hard_filters_raw = _mapping(raw, "hard_filters")
    blacklist_raw = hard_filters_raw.get("blacklist_codes")
    if not isinstance(blacklist_raw, list) or any(
        not isinstance(code, str) or len(code) != 6 or not code.isdigit() for code in blacklist_raw
    ):
        raise ConfigurationError("hard_filters.blacklist_codes must contain six-digit codes")
    if len(blacklist_raw) != len(set(blacklist_raw)):
        raise ConfigurationError("hard_filters.blacklist_codes must be unique")
    rules_raw = raw.get("risk_rules")
    if not isinstance(rules_raw, list):
        raise ConfigurationError("risk_rules must be a list")
    risk_rules = tuple(_parse_risk_rule(item, index) for index, item in enumerate(rules_raw))
    dimension_weights = _nested_number_mapping(raw, "dimension_weights")
    local_strategy_weights = _nested_number_mapping(raw, "local_strategy_weights")
    board_candidate_weights = _triple_nested_number_mapping(raw, "board_candidate_weights")
    board_local_strategy_weights = _triple_nested_number_mapping(raw, "board_local_strategy_weights")
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
        schema_version=10,
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
            maximum_board_fraction=_number(selection_raw, "maximum_board_fraction", minimum=0.01, maximum=1.0),
            competition_group_limits={
                name: int(value) for name, value in _number_mapping(selection_raw, "competition_group_limits").items()
            },
            candidate_min_score=_number(selection_raw, "candidate_min_score", minimum=0.0, maximum=100.0),
            minimum_board_reliability=_number(selection_raw, "minimum_board_reliability", minimum=0.0, maximum=1.0),
        ),
        candidate_weights=_number_mapping(raw, "candidate_weights"),
        hard_filters=HardFilterSettings(
            blacklist_codes=tuple(blacklist_raw),
            structured_risk_thresholds=_number_mapping(hard_filters_raw, "structured_risk_thresholds"),
        ),
        today_news_signal=today_news_signal,
        tomorrow_tail_signal=tomorrow_tail_signal,
        d25_signal=d25_signal,
        long_research=long_research,
        dimension_weights=dimension_weights,
        local_strategy_weights=local_strategy_weights,
        board_policy_version=_text(raw, "board_policy_version"),
        board_candidate_weights=board_candidate_weights,
        board_local_strategy_weights=board_local_strategy_weights,
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
        local_trigger_enabled=(_boolean(raw, "local_trigger_enabled") if "local_trigger_enabled" in raw else True),
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


__all__ = [
    "ApiSettings",
    "ConfigurationError",
    "DeepSeekSettings",
    "FusionSettings",
    "FactorDefinition",
    "HardFilterSettings",
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
