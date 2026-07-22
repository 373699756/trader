"""Executable factor registry parsing and contract validation."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping

from trader.infra.market_data.features import FEATURE_SCHEMA_NAMES, FEATURE_SCHEMA_VERSION
from trader.infra.settings_models import FactorDefinition, StrategySettings
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
    number as _number,
)
from trader.infra.settings_parser import (
    text as _text,
)


def _validate_feature_schema_contract(settings: StrategySettings) -> None:
    version = settings.factor_contract.get("feature_schema_version")
    if not isinstance(version, str):
        raise ConfigurationError("factor_contract.feature_schema_version is required")
    if version != FEATURE_SCHEMA_VERSION:
        raise ConfigurationError(
            f"factor_contract.feature_schema_version mismatch: expected={FEATURE_SCHEMA_VERSION}, got={version}"
        )
    configured_names = settings.factor_contract.get("feature_names")
    if configured_names is not None:
        if not isinstance(configured_names, list):
            raise ConfigurationError("factor_contract.feature_names must be a list")
        configured = set(configured_names)
        required = set(FEATURE_SCHEMA_NAMES)
        missing = sorted(required - configured)
        extra = sorted(configured - required)
        if missing or extra:
            raise ConfigurationError(
                f"factor_contract.feature_names mismatch configured feature schema: missing={missing}, extra={extra}"
            )
    if (registered_names := settings.factor_contract.get("feature_schema_expected")) is not None:
        if not isinstance(registered_names, int):
            raise ConfigurationError("factor_contract.feature_schema_expected must be int")
        if registered_names != len(FEATURE_SCHEMA_NAMES):
            raise ConfigurationError(
                f"factor_contract.feature_schema_expected mismatch: expected={len(FEATURE_SCHEMA_NAMES)}, got={registered_names}"
            )


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
            "strategies": ("d25", "long"),
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
            "strategies": ("d25", "long"),
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
            "strategies": ("d25", "long"),
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
        "shareholder_reduction_level": {
            **risk_common,
            "raw_inputs": ("validated_reduction_announcements",),
            "formula": "controller_or_actual_controller=3; five_percent_holder_or_director=2; other_disclosed_holder=1; terminated_unimplemented=0; source_failure=missing",
            "unit": "severity_level",
            "observation_time": "latest_published_before_observation",
            "lookback_window": 180,
            "minimum_samples": 0,
            "normalization": "configured_severity_0_3",
            "missing_policy": "missing_and_record",
            "output_range": (0.0, 3.0),
        },
        "unlock_risk": {
            **risk_common,
            "raw_inputs": ("upcoming_90d_TOTAL_RATIO",),
            "formula": "severity(sum(upcoming_90d_TOTAL_RATIO*100),[1,5,10]); source_failure=missing",
            "unit": "severity_level",
            "observation_time": "latest_published_before_observation",
            "lookback_window": 90,
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
