"""Cross-field strategy and risk-table validation."""

from __future__ import annotations

from collections.abc import Mapping

from trader.domain.market.factors import PRODUCTION_FACTOR_IDS
from trader.domain.market.research import MarketRegimePolicy
from trader.domain.review.rules import DEEPSEEK_STRUCTURED_RISK_CODES, deepseek_risk_rule_code
from trader.infra.settings.factor_validation import (
    _validate_feature_schema_contract,
    _validate_long_research_factor_contract,
    _validate_tomorrow_tail_factor_contract,
    _validate_weight_sum,
)
from trader.infra.settings.models import FactorDefinition, RiskRuleSettings, StrategySettings
from trader.infra.settings.parser import (
    ConfigurationError,
)

_FIXED_CANDIDATE_WEIGHTS = {
    "liquidity": 7 / 18,
    "short_momentum": 5 / 18,
    "trend": 4 / 18,
    "data_completeness": 2 / 18,
}
_FIXED_DIMENSION_WEIGHTS = {
    "today": {
        "value_quality": 2 / 17,
        "financial_health": 2 / 17,
        "market_flow": 8 / 17,
        "industry_policy": 0.0,
        "risk_quality": 5 / 17,
    },
    "tomorrow": {
        "value_quality": 3 / 16,
        "financial_health": 4 / 16,
        "market_flow": 5 / 16,
        "industry_policy": 0.0,
        "risk_quality": 4 / 16,
    },
    "d25": {
        "value_quality": 4 / 16,
        "financial_health": 5 / 16,
        "market_flow": 4 / 16,
        "industry_policy": 0.0,
        "risk_quality": 3 / 16,
    },
}
_FIXED_TODAY_BOARD_CANDIDATE_WEIGHTS = {
    "liquidity": 6 / 17,
    "intraday_structure": 5 / 17,
    "turnover_state": 4 / 17,
    "data_completeness": 2 / 17,
}
_FIXED_BOARD_CANDIDATE_WEIGHTS: dict[str, dict[str, dict[str, float]]] = {
    "today": {
        "main": _FIXED_TODAY_BOARD_CANDIDATE_WEIGHTS,
        "chinext": _FIXED_TODAY_BOARD_CANDIDATE_WEIGHTS,
        "star": _FIXED_TODAY_BOARD_CANDIDATE_WEIGHTS,
    },
    "tomorrow": {
        "main": {"liquidity": 7 / 17, "trend": 5 / 17, "stability": 3 / 17, "data_completeness": 2 / 17},
        "chinext": {"liquidity": 4 / 14, "trend": 5 / 14, "stability": 3 / 14, "data_completeness": 2 / 14},
        "star": {"liquidity": 5 / 17, "trend": 6 / 17, "stability": 4 / 17, "data_completeness": 2 / 17},
    },
    "d25": {
        "main": {
            "liquidity": 6 / 16,
            "trend": 4 / 16,
            "stability": 3 / 16,
            "execution": 2 / 16,
            "data_completeness": 1 / 16,
        },
        "chinext": {
            "liquidity": 4 / 14,
            "trend": 4 / 14,
            "stability": 2 / 14,
            "execution": 3 / 14,
            "data_completeness": 1 / 14,
        },
        "star": {
            "liquidity": 5 / 17,
            "trend": 6 / 17,
            "stability": 3 / 17,
            "execution": 2 / 17,
            "data_completeness": 1 / 17,
        },
    },
}
_FIXED_TODAY_BOARD_LOCAL_WEIGHTS = {
    "intraday_structure": 3 / 8,
    "turnover_state": 2 / 8,
    "liquidity_execution": 2 / 8,
    "stability": 1 / 8,
}
_FIXED_BOARD_LOCAL_WEIGHTS: dict[str, dict[str, dict[str, float]]] = {
    "today": {
        "main": _FIXED_TODAY_BOARD_LOCAL_WEIGHTS,
        "chinext": _FIXED_TODAY_BOARD_LOCAL_WEIGHTS,
        "star": _FIXED_TODAY_BOARD_LOCAL_WEIGHTS,
    },
    "tomorrow": {
        "main": {
            "tail_structure": 3 / 18,
            "turnover_flow": 1 / 18,
            "trend": 4 / 18,
            "stability": 5 / 18,
            "market_state": 2 / 18,
            "entry_quality": 3 / 18,
        },
        "chinext": {
            "tail_structure": 4 / 16,
            "turnover_flow": 3 / 16,
            "trend": 3 / 16,
            "stability": 2 / 16,
            "market_state": 1 / 16,
            "entry_quality": 3 / 16,
        },
        "star": {
            "tail_structure": 3 / 18,
            "turnover_flow": 1 / 18,
            "trend": 5 / 18,
            "stability": 5 / 18,
            "market_state": 1 / 18,
            "entry_quality": 3 / 18,
        },
    },
    "d25": {
        "main": {
            "trend": 5 / 17,
            "quality_value": 5 / 17,
            "stability": 3 / 17,
            "flow_liquidity": 2 / 17,
            "entry_quality": 2 / 17,
        },
        "chinext": {
            "trend": 4 / 14,
            "quality_value": 2 / 14,
            "stability": 2 / 14,
            "flow_liquidity": 4 / 14,
            "entry_quality": 2 / 14,
        },
        "star": {
            "trend": 6 / 17,
            "quality_value": 5 / 17,
            "stability": 3 / 17,
            "flow_liquidity": 2 / 17,
            "entry_quality": 1 / 17,
        },
    },
}


def _validate_strategy_settings(settings: StrategySettings) -> None:
    if settings.deepseek_risk_mapping_version != "deepseek_local_risk_rules_2026_08":
        raise ConfigurationError("unsupported DeepSeek risk mapping version")
    _validate_filter_fusion_selection(settings)
    _validate_signal_policies(settings)
    _validate_strategy_weights(settings)
    _validate_risk_registry(settings)


def _validate_filter_fusion_selection(settings: StrategySettings) -> None:
    _validate_hard_filters(settings)
    _validate_fusion(settings)
    _validate_selection(settings)


def _validate_hard_filters(settings: StrategySettings) -> None:
    expected_hard_filter_thresholds = {
        "major_shareholder_reduction": 0.0,
        "financial_fraud_history": 0.0,
        "official_investigation_history": 0.0,
        "major_illegal_history": 0.0,
        "fund_occupation_history": 0.0,
        "illegal_guarantee_history": 0.0,
        "forced_delisting_risk": 0.0,
        "unlock_risk": 0.0,
        "pledge_risk": 0.0,
        "financial_deterioration": 0.5,
    }
    if dict(settings.hard_filters.structured_risk_thresholds) != expected_hard_filter_thresholds:
        raise ConfigurationError("hard filter structured risk thresholds must match section 9")
    hard_filtered_factors = set(expected_hard_filter_thresholds)
    if any(rule.local_trigger_enabled for rule in settings.risk_rules if rule.trigger_factor in hard_filtered_factors):
        raise ConfigurationError("hard-filtered structured risks cannot also trigger local penalties")


def _validate_fusion(settings: StrategySettings) -> None:
    if abs(settings.fusion.local_weight + settings.fusion.deepseek_weight - 1.0) > 1e-9:
        raise ConfigurationError("fusion weights must sum to 1.0")
    if abs(settings.fusion.local_weight - 0.68) > 1e-9 or abs(settings.fusion.deepseek_weight - 0.32) > 1e-9:
        raise ConfigurationError("fusion weights are fixed at 0.68 and 0.32")
    if settings.fusion.version != "fusion_local68_deepseek32":
        raise ConfigurationError("unsupported fusion version")
    if settings.fusion.score_decimals != 2:
        raise ConfigurationError("fusion score_decimals must be 2")
    if settings.fusion.rounding != "ROUND_HALF_UP":
        raise ConfigurationError("unsupported score rounding mode")
    if settings.fusion.local_risk_cap != 25.0 or settings.fusion.deepseek_risk_cap != 30.0:
        raise ConfigurationError("risk caps are fixed at 25 local and 30 DeepSeek")
    if settings.fusion.confidence_coverage_min != 0.5 or settings.fusion.minimum_known_dimensions != 2:
        raise ConfigurationError("fusion coverage and known-dimension gates are fixed at 0.5 and 2")


def _validate_selection(settings: StrategySettings) -> None:
    if settings.selection.default_top_k > settings.selection.maximum_top_k:
        raise ConfigurationError("default_top_k cannot exceed maximum_top_k")
    if settings.selection.default_top_k != 6 or settings.selection.maximum_top_k != 12:
        raise ConfigurationError("active selection limits are fixed at 6 formal and 6 observation")
    if settings.board_policy_version != "board_policy_score_first_2026_07":
        raise ConfigurationError("unsupported board policy version")
    if settings.selection.maximum_board_fraction != 0.6:
        raise ConfigurationError("maximum board fraction is fixed at 0.6")
    if settings.selection.maximum_per_industry != 2:
        raise ConfigurationError("final recommendation industry limit must be 2")
    if settings.selection.competition_group_limits:
        raise ConfigurationError("competition group limits must be disabled")
    if settings.selection.candidate_min_score != 50.0 or settings.selection.minimum_board_reliability != 0.85:
        raise ConfigurationError("candidate score and board reliability gates are fixed at 50 and 0.85")
    if settings.selection.review_candidate_limit != 28:
        raise ConfigurationError("DeepSeek review candidate limit must be 28")
    if settings.selection.observation_margin != 5.0:
        raise ConfigurationError("selection observation margin is fixed at 5")


def _validate_signal_policies(settings: StrategySettings) -> None:
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
    market_regime = settings.market_regime
    if market_regime != MarketRegimePolicy(
        risk_on_breadth_min=60.0,
        risk_off_breadth_max=40.0,
    ):
        raise ConfigurationError("market regime boundaries are fixed at 60/40")
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


def _validate_strategy_weights(settings: StrategySettings) -> None:
    _validate_weight_sum("candidate_weights", settings.candidate_weights)
    required_candidate_weights = {
        "liquidity",
        "short_momentum",
        "trend",
        "data_completeness",
    }
    if set(settings.candidate_weights) != required_candidate_weights:
        raise ConfigurationError("candidate_weights contains unsupported components")
    _validate_fixed_vector("candidate_weights", settings.candidate_weights, _FIXED_CANDIDATE_WEIGHTS)
    required_thresholds = {"today_main", "today_late", "tomorrow", "d25"}
    if set(settings.selection.thresholds) != required_thresholds:
        raise ConfigurationError("selection thresholds must define today_main, today_late, tomorrow and d25")
    if dict(settings.selection.thresholds) != {
        "today_main": 70.0,
        "today_late": 76.0,
        "tomorrow": 78.0,
        "d25": 76.0,
    }:
        raise ConfigurationError("current selection thresholds must be 70/76/78/76")
    required_strategies = {"today", "tomorrow", "d25"}
    _validate_dimension_weights(settings, required_strategies)
    _validate_board_weights(settings)


def _validate_dimension_weights(settings: StrategySettings, required_strategies: set[str]) -> None:
    if set(settings.dimension_weights) != required_strategies:
        raise ConfigurationError("dimension_weights must define today, tomorrow and d25")
    required_dimensions = {
        "value_quality",
        "financial_health",
        "market_flow",
        "industry_policy",
        "risk_quality",
    }
    for strategy, weights in settings.dimension_weights.items():
        _validate_weight_sum(f"dimension_weights.{strategy}", weights)
        if set(weights) != required_dimensions:
            raise ConfigurationError(f"dimension_weights.{strategy} must define the five review dimensions")
        _validate_fixed_vector(
            f"dimension_weights.{strategy}",
            weights,
            _FIXED_DIMENSION_WEIGHTS[strategy],
        )


def _validate_risk_registry(settings: StrategySettings) -> None:
    _validate_risk_registry_contract(settings)
    _validate_short_risk_contract(settings)


def _validate_risk_registry_contract(settings: StrategySettings) -> None:
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
    _validate_long_research_factor_contract(settings)
    _validate_feature_schema_contract(settings)
    required_risk_codes = {
        "near_limit_crowding",
        "price_volume_divergence",
        "high_volatility",
        "short_term_overheat",
        "intraday_reversal",
        "liquidity_contraction",
        "trend_breakdown",
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
    _validate_deepseek_risk_mapping_targets(required_risk_codes)
    for rule in settings.risk_rules:
        definition = settings.factor_registry.get(rule.trigger_factor)
        if definition is None:
            raise ConfigurationError(f"risk rule {rule.risk_code} trigger factor is not registered")
        if not set(rule.strategies).issubset(definition.strategies):
            raise ConfigurationError(f"risk rule {rule.risk_code} uses a factor outside its registered strategies")


def _validate_deepseek_risk_mapping_targets(registered_risk_codes: set[str]) -> None:
    for risk_code in DEEPSEEK_STRUCTURED_RISK_CODES:
        for severity in ("low", "medium", "high"):
            mapped_code = deepseek_risk_rule_code(risk_code, severity)
            if mapped_code not in registered_risk_codes:
                raise ConfigurationError("DeepSeek structured risks must map completely to local risk rules")


def _validate_short_risk_contract(settings: StrategySettings) -> None:
    short_risk_penalties = {
        "near_limit_crowding": 5.0,
        "price_volume_divergence": 4.0,
        "high_volatility": 3.0,
        "short_term_overheat": 3.0,
        "intraday_reversal": 4.0,
        "liquidity_contraction": 3.0,
        "trend_breakdown": 3.0,
    }
    short_rules = {rule.risk_code: rule for rule in settings.risk_rules if rule.risk_code in short_risk_penalties}
    if any(
        rule.penalty != short_risk_penalties[code]
        or set(rule.strategies) != {"today", "tomorrow", "d25"}
        or not rule.local_trigger_enabled
        or rule.combination_mode != "additive"
        for code, rule in short_rules.items()
    ):
        raise ConfigurationError(
            "current short risk rules must use fixed strategies, additive groups and 5/4/3/3/4/3/3"
        )
    _validate_short_risk_rules(short_rules)
    _validate_short_risk_factors(settings.factor_registry)
    group_modes: dict[str, str] = {}
    for rule in settings.risk_rules:
        existing = group_modes.setdefault(rule.group, rule.combination_mode)
        if existing != rule.combination_mode:
            raise ConfigurationError(f"risk group {rule.group} mixes combination modes")


def _validate_short_risk_rules(rules: Mapping[str, RiskRuleSettings]) -> None:
    identity_fields = ("stock_code", "risk_code", "actual", "source", "trade_date")
    expected = {
        "near_limit_crowding": ("medium", 5.0, "limit_proximity", "gte", (0.75,), 4, "market_crowding"),
        "price_volume_divergence": (
            "medium",
            4.0,
            "price_volume_divergence",
            "gte",
            (0.5,),
            24,
            "market_structure_divergence",
        ),
        "high_volatility": (
            "low",
            3.0,
            "volatility_20d",
            "gte",
            (4.0,),
            24,
            "market_structure_volatility",
        ),
        "short_term_overheat": (
            "low",
            3.0,
            "short_term_overheat",
            "gte",
            (0.5,),
            24,
            "market_structure_overheat",
        ),
        "intraday_reversal": (
            "medium",
            4.0,
            "intraday_reversal",
            "gte",
            (0.5,),
            4,
            "market_structure_reversal",
        ),
        "liquidity_contraction": (
            "low",
            3.0,
            "liquidity_contraction",
            "gte",
            (0.5,),
            4,
            "market_structure_liquidity",
        ),
        "trend_breakdown": (
            "low",
            3.0,
            "trend_breakdown",
            "gte",
            (0.5,),
            24,
            "market_structure_breakdown",
        ),
    }
    if set(rules) != set(expected):
        raise ConfigurationError("current short risk rule set is incomplete")
    for code, contract in expected.items():
        rule = rules[code]
        actual = (
            rule.severity,
            rule.penalty,
            rule.trigger_factor,
            rule.trigger_operator,
            rule.trigger_thresholds,
            rule.evidence_ttl_hours,
            rule.group,
        )
        if actual != contract:
            raise ConfigurationError(f"risk rule {code} does not match the current fixed trigger contract")
        if (
            rule.minimum_confidence != 0.7
            or rule.veto
            or rule.allowed_evidence_types != ("structured_point_in_time",)
            or set(rule.strategies) != {"today", "tomorrow", "d25"}
            or rule.combination_mode != "additive"
            or rule.risk_fact_id_fields != identity_fields
            or not rule.local_trigger_enabled
        ):
            raise ConfigurationError(f"risk rule {code} has invalid current audit or evidence settings")


def _validate_short_risk_factors(factors: Mapping[str, FactorDefinition]) -> None:
    expected = {
        "price_volume_divergence": (
            ("return_5d", "intraday_amount", "amount_median_20d"),
            "1 if (return_5d>0 and intraday_amount/amount_median_20d<0.8) or "
            "(return_5d<0 and intraday_amount/amount_median_20d>1.2) else 0",
            20,
            20,
            "mixed_anchor_unadjusted_history_forward",
            "2",
        ),
        "short_term_overheat": (
            ("return_5d", "return_10d", "ma20_deviation_pct"),
            "1 if return_5d>=12 or return_10d>=20 or ma20_deviation_pct>=15 else 0",
            20,
            6,
            "forward",
            "1",
        ),
        "intraday_reversal": (
            ("unadjusted_intraday_high", "unadjusted_price", "close_location", "completed_trading_minutes"),
            "1 if completed_trading_minutes>=30 and (high-price)/high*100>=3 and close_location<=35 else 0",
            0,
            30,
            "none",
            "1",
        ),
        "liquidity_contraction": (
            ("volume_ratio", "intraday_amount", "amount_median_20d"),
            "1 if volume_ratio<=0.6 or intraday_amount/amount_median_20d<=0.6 else 0",
            20,
            20,
            "none",
            "1",
        ),
        "trend_breakdown": (
            ("ma20_deviation_pct", "ma_slope", "return_5d"),
            "1 if ma20_deviation_pct<0 and ma_slope<50 and return_5d<0 else 0",
            20,
            20,
            "forward",
            "1",
        ),
    }
    for name, contract in expected.items():
        factor = factors.get(name)
        if factor is None:
            raise ConfigurationError(f"missing current risk factor definition: {name}")
        actual = (
            factor.raw_inputs,
            factor.formula,
            factor.lookback_window,
            factor.minimum_samples,
            factor.adjustment,
            factor.version,
        )
        if actual != contract:
            raise ConfigurationError(f"risk factor {name} does not match the current fixed formula")


def _validate_board_weights(settings: StrategySettings) -> None:
    strategies = {"today", "tomorrow", "d25"}
    boards = {"main", "chinext", "star"}
    if set(settings.board_candidate_weights) != strategies or set(settings.board_local_strategy_weights) != strategies:
        raise ConfigurationError("board weights must define today, tomorrow and d25")
    candidate_components = {
        "today": {"liquidity", "intraday_structure", "turnover_state", "data_completeness"},
        "tomorrow": {"liquidity", "trend", "stability", "data_completeness"},
        "d25": {"liquidity", "trend", "stability", "execution", "data_completeness"},
    }
    local_components = {
        "today": {"intraday_structure", "turnover_state", "liquidity_execution", "stability"},
        "tomorrow": {
            "tail_structure",
            "turnover_flow",
            "trend",
            "stability",
            "market_state",
            "entry_quality",
        },
        "d25": {"trend", "quality_value", "stability", "flow_liquidity", "entry_quality"},
    }
    for strategy in strategies:
        candidate_boards = settings.board_candidate_weights[strategy]
        local_boards = settings.board_local_strategy_weights[strategy]
        if set(candidate_boards) != boards or set(local_boards) != boards:
            raise ConfigurationError(f"board weights for {strategy} must define all three boards")
        for board in boards:
            _validate_weight_sum(f"board_candidate_weights.{strategy}.{board}", candidate_boards[board])
            _validate_weight_sum(f"board_local_strategy_weights.{strategy}.{board}", local_boards[board])
            if set(candidate_boards[board]) != candidate_components[strategy]:
                raise ConfigurationError(f"board candidate components for {strategy}.{board} are invalid")
            if set(local_boards[board]) != local_components[strategy]:
                raise ConfigurationError(f"board local components for {strategy}.{board} are invalid")
            _validate_fixed_vector(
                f"board_candidate_weights.{strategy}.{board}",
                candidate_boards[board],
                _FIXED_BOARD_CANDIDATE_WEIGHTS[strategy][board],
            )
            _validate_fixed_vector(
                f"board_local_strategy_weights.{strategy}.{board}",
                local_boards[board],
                _FIXED_BOARD_LOCAL_WEIGHTS[strategy][board],
            )


def _validate_fixed_vector(
    name: str,
    actual: Mapping[str, float],
    expected: Mapping[str, float],
) -> None:
    if set(actual) != set(expected) or any(abs(actual[key] - expected[key]) > 1e-12 for key in expected):
        raise ConfigurationError(f"{name} must match its fixed vector")
