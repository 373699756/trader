from __future__ import annotations

import hashlib
import json
import math
from typing import Dict

from . import config
from .normalization import coerce_number


EXECUTION_POLICY_FAMILY = "cn_a_close_auction_execution_v2"


def build_execution_policy(strategy_name: str, market: str = "") -> Dict[str, object]:
    """Return the complete, immutable policy used to label a signal."""
    holding_days = 5 if strategy_name in {"tomorrow_picks", "swing_picks"} else 1
    prefix = "TOMORROW_AUXILIARY" if strategy_name == "tomorrow_picks" else "SWING_VALIDATION"
    exit_policy = {
        "holding_days": holding_days,
        "take_profit_pct": _configured_pct(prefix, "TAKE_PROFIT_PCT", 8.0),
        "stop_loss_pct": _configured_pct(prefix, "STOP_LOSS_PCT", 5.0),
        "trailing_stop_pct": _configured_pct(prefix, "TRAILING_STOP_PCT", 4.0),
        "sealed_limit_down": "delay_until_first_tradable_open",
        "earliest_exit_offset_days": 1 if strategy_name == "swing_picks" else 0,
    }
    if strategy_name not in {"tomorrow_picks", "swing_picks"}:
        exit_policy = {
            "holding_days": holding_days,
            "take_profit_pct": 0.0,
            "stop_loss_pct": 0.0,
            "trailing_stop_pct": 0.0,
            "sealed_limit_down": "delay_until_first_tradable_open",
        }

    body = {
        "schema_version": 2,
        "policy_family": EXECUTION_POLICY_FAMILY,
        "strategy_name": str(strategy_name or ""),
        "market": str(market or ""),
        "entry": {
            "timing": (
                "same_trade_day_close_auction"
                if strategy_name == "tomorrow_picks"
                else "next_trade_day_open"
            ),
            "price_field": (
                "signal_day_raw_close"
                if strategy_name == "tomorrow_picks"
                else "open_with_close_fallback"
            ),
            "sealed_limit_up": "unfilled",
            "suspension": "unfilled_until_tradable",
            "signal_cutoff": (
                str(getattr(config, "TOMORROW_SIGNAL_CUTOFF_TIME", "14:55"))
                if strategy_name == "tomorrow_picks"
                else None
            ),
            "order_window": (
                "{}-{}".format(
                    getattr(config, "TOMORROW_CLOSE_AUCTION_START_TIME", "14:55"),
                    getattr(config, "TOMORROW_CLOSE_AUCTION_END_TIME", "15:00"),
                )
                if strategy_name == "tomorrow_picks"
                else None
            ),
        },
        "exit": exit_policy,
        "price_limits": {
            "main_board_pct": 10.0,
            "chinext_star_pct": 20.0,
            "entry_limit_up_action": "unfilled",
            "exit_limit_down_action": "defer",
        },
        "cost": {
            "fee_round_trip_pct": coerce_number(getattr(config, "VALIDATION_TRADE_COST_PCT", 0.25), 0.25),
            "liquidity_slippage_pct": {
                "turnover_ge_1b": coerce_number(
                    getattr(config, "VALIDATION_SLIPPAGE_HIGH_TURNOVER_PCT", 0.05), 0.05
                ),
                "turnover_ge_300m": coerce_number(
                    getattr(config, "VALIDATION_SLIPPAGE_MID_TURNOVER_PCT", 0.12), 0.12
                ),
                "turnover_ge_100m": coerce_number(
                    getattr(config, "VALIDATION_SLIPPAGE_LOW_TURNOVER_PCT", 0.25), 0.25
                ),
                "turnover_lt_100m": coerce_number(
                    getattr(config, "VALIDATION_SLIPPAGE_MICRO_TURNOVER_PCT", 0.45), 0.45
                ),
            },
            "tail_auction": {
                "enabled": bool(getattr(config, "ENABLE_TAIL_AUCTION_SLIPPAGE", False)),
                "liquidity_ratio": coerce_number(getattr(config, "TAIL_AUCTION_LIQUIDITY_RATIO", 0.05), 0.05),
                "max_extra_pct": coerce_number(
                    getattr(config, "TAIL_AUCTION_MAX_EXTRA_SLIPPAGE_PCT", 0.8), 0.8
                ),
            },
            "market_impact": {
                "enabled": bool(getattr(config, "ENABLE_MARKET_IMPACT", False)),
                "coefficient": coerce_number(getattr(config, "MARKET_IMPACT_COEFFICIENT", 0.1), 0.1),
                "max_cost_pct": coerce_number(getattr(config, "MARKET_IMPACT_MAX_COST_PCT", 5.0), 5.0),
            },
            "scenario_multipliers": {"low": 0.5, "medium": 1.0, "high": 2.0},
        },
        "portfolio": {
            "capital": coerce_number(getattr(config, "VALIDATION_PORTFOLIO_CAPITAL", 1_000_000), 1_000_000),
            "weighting": (
                "frozen_equal_weight_top_k"
                if strategy_name == "tomorrow_picks"
                else "strategy_suggested_or_default"
            ),
            "default_target_weight_pct": (
                round(100.0 / max(1, int(getattr(config, "PRODUCTION_TOP_K", 5))), 6)
                if strategy_name == "tomorrow_picks"
                else coerce_number(getattr(config, "VALIDATION_DEFAULT_POSITION_PCT", 10.0), 10.0)
            ),
            "board_lot": 100,
        },
        "missing_or_unfilled": {
            "unfilled_entry_return": 0.0,
            "open_position_return": None,
            "exit_pending_action": "retry_until_tradable",
            "data_source_failure": "unknown",
            "promotion_eligible": False,
        },
        "delisting": {
            "before_entry": "unfilled",
            "after_entry": "last_tradable_price",
            "missing_last_tradable_price": "unknown",
            "synthetic_default_return": False,
        },
    }
    body["exit"]["primary_timing"] = (
        "next_trade_day_close_auction" if strategy_name == "tomorrow_picks" else "dynamic_2_5d"
        if strategy_name == "swing_picks"
        else "next_trade_day_close"
    )
    body["exit"]["primary_holding_days"] = 1 if strategy_name != "swing_picks" else holding_days
    body["exit"]["auxiliary_holding_days"] = holding_days
    versioned_body = {key: value for key, value in body.items() if key != "market"}
    canonical = json.dumps(versioned_body, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    body["policy_version"] = "{}.{}".format(
        EXECUTION_POLICY_FAMILY,
        hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12],
    )
    return body


def policy_from_signal(signal, strategy_name: str = "") -> Dict[str, object]:
    raw = _mapping_get(signal, "execution_policy_json", "")
    if isinstance(raw, dict):
        policy = dict(raw)
    else:
        try:
            policy = json.loads(str(raw or "{}"))
        except Exception:
            policy = {}
    if not isinstance(policy, dict) or not policy.get("policy_version"):
        policy = build_execution_policy(
            strategy_name or str(_mapping_get(signal, "strategy_name", "")),
            str(_mapping_get(signal, "market", "")),
        )
    return policy


def liquidity_slippage_pct(turnover: float, policy: Dict[str, object] = None) -> float:
    tiers = ((policy or {}).get("cost") or {}).get("liquidity_slippage_pct") or {}
    amount = coerce_number(turnover)
    if amount >= 1_000_000_000:
        return coerce_number(tiers.get("turnover_ge_1b"), getattr(config, "VALIDATION_SLIPPAGE_HIGH_TURNOVER_PCT", 0.05))
    if amount >= 300_000_000:
        return coerce_number(tiers.get("turnover_ge_300m"), getattr(config, "VALIDATION_SLIPPAGE_MID_TURNOVER_PCT", 0.12))
    if amount >= 100_000_000:
        return coerce_number(tiers.get("turnover_ge_100m"), getattr(config, "VALIDATION_SLIPPAGE_LOW_TURNOVER_PCT", 0.25))
    return coerce_number(tiers.get("turnover_lt_100m"), getattr(config, "VALIDATION_SLIPPAGE_MICRO_TURNOVER_PCT", 0.45))


def target_weight_pct(row, policy: Dict[str, object] = None) -> float:
    portfolio = (policy or {}).get("portfolio") or {}
    if portfolio.get("weighting") == "frozen_equal_weight_top_k":
        return round(
            max(0.0, coerce_number(portfolio.get("default_target_weight_pct"), 20.0)),
            6,
        )
    suggested_weight = _mapping_get(row, "suggested_weight", None)
    value = coerce_number(suggested_weight, None)
    if suggested_weight is None:
        trade_action = _mapping_get(row, "trade_action", {})
        if isinstance(trade_action, dict):
            position = coerce_number(trade_action.get("position_size"), None)
            if position is not None and position > 0:
                value = position * 100.0 if position <= 1.0 else position
    if value is None:
        value = coerce_number(
            portfolio.get("default_target_weight_pct"),
            getattr(config, "VALIDATION_DEFAULT_POSITION_PCT", 10.0),
        )
    return round(max(0.0, value), 6)


def estimated_order_amount(row, policy: Dict[str, object] = None) -> float:
    portfolio = (policy or {}).get("portfolio") or {}
    capital = max(
        0.0,
        coerce_number(portfolio.get("capital"), getattr(config, "VALIDATION_PORTFOLIO_CAPITAL", 1_000_000.0)),
    )
    return capital * target_weight_pct(row, policy) / 100.0


def tail_auction_slippage_pct(row, base_slippage: float = 0.0, policy: Dict[str, object] = None) -> float:
    tail = ((policy or {}).get("cost") or {}).get("tail_auction") or {}
    enabled = tail.get("enabled") if "enabled" in tail else bool(getattr(config, "ENABLE_TAIL_AUCTION_SLIPPAGE", False))
    if not enabled:
        return 0.0
    base = max(0.0, coerce_number(base_slippage))
    daily_turnover = coerce_number(_mapping_get(row, "turnover"))
    order_amount = estimated_order_amount(row, policy)
    if daily_turnover <= 0 or order_amount <= 0:
        return round(base + 0.5, 4)
    liquidity_ratio = max(
        0.0001,
        coerce_number(tail.get("liquidity_ratio"), getattr(config, "TAIL_AUCTION_LIQUIDITY_RATIO", 0.05)),
    )
    effective_liquidity = daily_turnover * liquidity_ratio
    max_extra = max(
        0.0,
        coerce_number(tail.get("max_extra_pct"), getattr(config, "TAIL_AUCTION_MAX_EXTRA_SLIPPAGE_PCT", 0.8)),
    )
    extra = min(max_extra, order_amount / effective_liquidity * 100.0 * 0.02)
    return round(base + extra, 4)


def market_impact_cost_pct(row, policy: Dict[str, object] = None) -> float:
    impact_policy = ((policy or {}).get("cost") or {}).get("market_impact") or {}
    enabled = (
        impact_policy.get("enabled")
        if "enabled" in impact_policy
        else bool(getattr(config, "ENABLE_MARKET_IMPACT", False))
    )
    if not enabled:
        return 0.0
    adv = coerce_number(_mapping_get(row, "adv_20d")) or coerce_number(_mapping_get(row, "turnover"))
    order_amount = estimated_order_amount(row, policy)
    if adv <= 0 or order_amount <= 0:
        return 0.0
    coefficient = max(
        0.0,
        coerce_number(impact_policy.get("coefficient"), getattr(config, "MARKET_IMPACT_COEFFICIENT", 0.1)),
    )
    max_cost = max(
        0.0,
        coerce_number(impact_policy.get("max_cost_pct"), getattr(config, "MARKET_IMPACT_MAX_COST_PCT", 5.0)),
    )
    return round(min(max_cost, coefficient * math.sqrt(max(0.0, order_amount / adv)) * 100.0), 4)


def execution_cost_components(row, policy: Dict[str, object] = None) -> Dict[str, float]:
    policy = policy or build_execution_policy(str(_mapping_get(row, "strategy_name", "")))
    fee = coerce_number(
        ((policy.get("cost") or {}).get("fee_round_trip_pct")),
        getattr(config, "VALIDATION_TRADE_COST_PCT", 0.25),
    )
    liquidity = liquidity_slippage_pct(coerce_number(_mapping_get(row, "turnover")), policy)
    tail = tail_auction_slippage_pct(row, policy=policy)
    impact = market_impact_cost_pct(row, policy=policy)
    return {
        "fee_pct": round(fee, 4),
        "slippage_pct": round(liquidity + tail, 4),
        "impact_pct": round(impact, 4),
        "total_pct": round(fee + liquidity + tail + impact, 4),
    }


def cost_scenarios(row, policy: Dict[str, object] = None) -> Dict[str, Dict[str, float]]:
    policy = policy or build_execution_policy(str(_mapping_get(row, "strategy_name", "")))
    components = execution_cost_components(row, policy)
    multipliers = ((policy.get("cost") or {}).get("scenario_multipliers") or {})
    scenarios: Dict[str, Dict[str, float]] = {}
    for name, fallback in (("low", 0.5), ("medium", 1.0), ("high", 2.0)):
        multiplier = max(0.0, coerce_number(multipliers.get(name), fallback))
        fee = components["fee_pct"]
        slippage = components["slippage_pct"] * multiplier
        impact = components["impact_pct"] * multiplier
        scenarios[name] = {
            "fee_pct": round(fee, 4),
            "slippage_pct": round(slippage, 4),
            "impact_pct": round(impact, 4),
            "total_pct": round(fee + slippage + impact, 4),
        }
    scenarios["base"] = dict(scenarios["medium"])
    return scenarios


def order_quantities(row, entry_price: float, policy: Dict[str, object] = None) -> Dict[str, float]:
    policy = policy or build_execution_policy(str(_mapping_get(row, "strategy_name", "")))
    portfolio = policy.get("portfolio") or {}
    capital = max(0.0, coerce_number(portfolio.get("capital")))
    target_weight = target_weight_pct(row, policy)
    target_notional = capital * target_weight / 100.0
    board_lot = max(1, int(coerce_number(portfolio.get("board_lot"), 100)))
    price = coerce_number(entry_price)
    quantity = int(target_notional / price / board_lot) * board_lot if price > 0 else 0
    return {
        "portfolio_capital": round(capital, 4),
        "target_weight_pct": round(target_weight, 6),
        "target_notional": round(target_notional, 4),
        "order_quantity": quantity,
    }


def _configured_pct(prefix: str, suffix: str, fallback: float) -> float:
    return max(0.0, coerce_number(getattr(config, "{}_{}".format(prefix, suffix), fallback), fallback))


def _mapping_get(row, key: str, default=None):
    if hasattr(row, "get"):
        return row.get(key, default)
    try:
        return row[key]
    except Exception:
        return default
