"""Cross-field strategy and risk-table validation."""

from __future__ import annotations

from trader.domain.factors import PRODUCTION_FACTOR_IDS
from trader.domain.research import D25SignalPolicy
from trader.infrastructure.settings_factor_validation import (
    _validate_d25_factor_contract,
    _validate_feature_schema_contract,
    _validate_long_research_factor_contract,
    _validate_tomorrow_tail_factor_contract,
    _validate_weight_sum,
)
from trader.infrastructure.settings_models import StrategySettings
from trader.infrastructure.settings_parser import (
    ConfigurationError,
)


def _validate_strategy_settings(settings: StrategySettings) -> None:
    expected_hard_filter_thresholds = {
        "negative_announcement_level": 0.0,
        "reduction_or_unlock": 0.0,
        "pledge_risk": 0.0,
        "financial_deterioration": 0.5,
    }
    if dict(settings.hard_filters.structured_risk_thresholds) != expected_hard_filter_thresholds:
        raise ConfigurationError("hard filter structured risk thresholds must match section 9")
    hard_filtered_factors = set(expected_hard_filter_thresholds)
    if any(rule.local_trigger_enabled for rule in settings.risk_rules if rule.trigger_factor in hard_filtered_factors):
        raise ConfigurationError("hard-filtered structured risks cannot also trigger local penalties")
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
    if set(settings.local_strategy_weights) != required_strategies:
        raise ConfigurationError("local_strategy_weights must define today, tomorrow, d25 and long")
    required_local_components: dict[str, set[str]] = {
        "today": {"momentum", "liquidity", "industry", "sentiment", "protection"},
        "tomorrow": {"liquidity", "momentum", "trend", "historical_edge", "execution", "tail_structure"},
        "d25": {"momentum", "trend", "liquidity", "execution", "not_overheated"},
        "long": {"value", "growth", "quality", "industry_policy", "protection"},
    }
    for strategy, weights in settings.local_strategy_weights.items():
        _validate_weight_sum(f"local_strategy_weights.{strategy}", weights)
        if set(weights) != required_local_components[strategy]:
            raise ConfigurationError(f"local_strategy_weights.{strategy} components are invalid")
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
    _validate_feature_schema_contract(settings)
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
