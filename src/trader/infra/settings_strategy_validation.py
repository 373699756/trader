"""Cross-field strategy and risk-table validation."""

from __future__ import annotations

from collections.abc import Mapping

from trader.domain.market.factors import PRODUCTION_FACTOR_IDS
from trader.domain.market.research import D25SignalPolicy
from trader.infra.settings_factor_validation import (
    _validate_d25_factor_contract,
    _validate_feature_schema_contract,
    _validate_long_research_factor_contract,
    _validate_tomorrow_tail_factor_contract,
    _validate_weight_sum,
)
from trader.infra.settings_models import FactorDefinition, RiskRuleSettings, StrategySettings
from trader.infra.settings_parser import (
    ConfigurationError,
)


def _validate_strategy_settings(settings: StrategySettings) -> None:
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
        "negative_announcement_level": 0.0,
        "shareholder_reduction_level": 0.0,
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
    if settings.fusion.version != "fusion_v2_local68_deepseek32":
        raise ConfigurationError("unsupported fusion version")
    if settings.fusion.score_decimals != 2:
        raise ConfigurationError("fusion score_decimals must be 2")
    if settings.fusion.rounding != "ROUND_HALF_UP":
        raise ConfigurationError("unsupported score rounding mode")
    if settings.fusion.local_risk_cap != 25.0 or settings.fusion.deepseek_risk_cap != 30.0:
        raise ConfigurationError("risk caps are fixed at 25 local and 30 DeepSeek")


def _validate_selection(settings: StrategySettings) -> None:
    if settings.selection.default_top_k > settings.selection.maximum_top_k:
        raise ConfigurationError("default_top_k cannot exceed maximum_top_k")
    if settings.board_policy_version != "board_policy_v17_downside_guard_ttd25_2026_07":
        raise ConfigurationError("unsupported board policy version")
    if settings.selection.maximum_board_fraction != 0.6:
        raise ConfigurationError("maximum board fraction is fixed at 0.6")
    if dict(settings.selection.competition_group_limits) != {"main": 3, "chinext": 2, "star": 2}:
        raise ConfigurationError("competition group limits must be main=3, chinext=2 and star=2")
    if settings.selection.candidate_min_score != 50.0 or settings.selection.minimum_board_reliability != 0.85:
        raise ConfigurationError("candidate score and board reliability gates are fixed at 50 and 0.85")


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


def _validate_strategy_weights(settings: StrategySettings) -> None:
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
    if dict(settings.selection.thresholds) != {
        "today_main": 70.0,
        "today_late": 76.0,
        "tomorrow": 78.0,
        "d25": 76.0,
    }:
        raise ConfigurationError("v16 selection thresholds must be 70/76/78/76")
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
    _validate_board_weights(settings)


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
    _validate_d25_factor_contract(settings)
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
    for rule in settings.risk_rules:
        definition = settings.factor_registry.get(rule.trigger_factor)
        if definition is None:
            raise ConfigurationError(f"risk rule {rule.risk_code} trigger factor is not registered")
        if not set(rule.strategies).issubset(definition.strategies):
            raise ConfigurationError(f"risk rule {rule.risk_code} uses a factor outside its registered strategies")


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
        raise ConfigurationError("v16 short risk rules must use fixed strategies, additive groups and 5/4/3/3/4/3/3")
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
        raise ConfigurationError("v16 short risk rule set is incomplete")
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
            raise ConfigurationError(f"risk rule {code} does not match the fixed v16 trigger contract")
        if (
            rule.minimum_confidence != 0.7
            or rule.veto
            or rule.allowed_evidence_types != ("structured_point_in_time",)
            or set(rule.strategies) != {"today", "tomorrow", "d25"}
            or rule.combination_mode != "additive"
            or rule.risk_fact_id_fields != identity_fields
            or not rule.local_trigger_enabled
        ):
            raise ConfigurationError(f"risk rule {code} has invalid v16 audit or evidence settings")


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
            raise ConfigurationError(f"missing v16 risk factor definition: {name}")
        actual = (
            factor.raw_inputs,
            factor.formula,
            factor.lookback_window,
            factor.minimum_samples,
            factor.adjustment,
            factor.version,
        )
        if actual != contract:
            raise ConfigurationError(f"risk factor {name} does not match the fixed v16 formula")


def _validate_board_weights(settings: StrategySettings) -> None:
    strategies = {"today", "tomorrow", "d25"}
    boards = {"main", "chinext", "star"}
    if set(settings.board_candidate_weights) != strategies or set(settings.board_local_strategy_weights) != strategies:
        raise ConfigurationError("board weights must define today, tomorrow and d25")
    candidate_components = {
        "today": {"liquidity", "intraday_structure", "turnover_state", "peer_gap", "data_completeness"},
        "tomorrow": {"liquidity", "peer_gap", "trend", "stability", "data_completeness"},
        "d25": {"liquidity", "residual_momentum", "trend", "stability", "execution", "data_completeness"},
    }
    local_components = {
        "today": {"intraday_structure", "turnover_state", "peer_gap", "liquidity_execution", "stability"},
        "tomorrow": {
            "tail_structure",
            "peer_leader",
            "turnover_flow",
            "trend",
            "stability",
            "market_state",
            "entry_quality",
        },
        "d25": {"residual_momentum", "trend", "quality_value", "stability", "flow_liquidity", "entry_quality"},
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
    if len({tuple(settings.board_candidate_weights["today"][board].items()) for board in boards}) != 1:
        raise ConfigurationError("today board candidate weights must be identical across boards")
    if len({tuple(settings.board_local_strategy_weights["today"][board].items()) for board in boards}) != 1:
        raise ConfigurationError("today board local weights must be identical across boards")
