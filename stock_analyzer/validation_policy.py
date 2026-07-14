from __future__ import annotations

import hashlib
import json
from typing import Dict

from . import config
from .execution_policy import (
    build_execution_policy,
    estimated_order_amount as _policy_estimated_order_amount,
    execution_cost_components as _policy_execution_cost_components,
    market_impact_cost_pct as _policy_market_impact_cost_pct,
    policy_from_signal,
    tail_auction_slippage_pct as _policy_tail_auction_slippage_pct,
)
from .normalization import coerce_number, normalize_code


PRIMARY_RETURN_BY_STRATEGY = {
    "short_term": ("signal_exit_return", 0, "信号时点至T日收盘延续收益（非新建仓交易）"),
    "tomorrow_picks": ("signal_exit_return", 1, "T日14:30后参考入场至T+1规则退出"),
    "swing_picks": ("signal_exit_return", 5, "T日14:30后参考入场至T+2-T+5规则退出"),
}

EXECUTABLE_PRIMARY_RETURN_BY_STRATEGY = {
    **PRIMARY_RETURN_BY_STRATEGY,
}


def current_strategy_version(strategy_name: str) -> str:
    return {
        "short_term": str(getattr(config, "SHORT_TERM_STRATEGY_VERSION", "today_picks_v1_remaining_session")),
        "tomorrow_picks": str(
            getattr(config, "TOMORROW_STRATEGY_VERSION", "tomorrow_picks_v12_post_1430_t1_exit")
        ),
        "swing_picks": str(getattr(config, "SWING_STRATEGY_VERSION", "swing_2_5d_v4_post_1430_entry")),
    }.get(str(strategy_name or ""), "")


def current_replay_strategy_version(strategy_name: str) -> str:
    suffix = str(getattr(config, "VALIDATION_REPLAY_VERSION_SUFFIX", "replay_v2_production"))
    return "{}_{}".format(str(strategy_name or ""), suffix) if strategy_name else ""


def primary_return_config(strategy_name: str):
    return PRIMARY_RETURN_BY_STRATEGY.get(
        strategy_name,
        ("signal_exit_return", 0, "同日信号至收盘"),
    )


def build_validation_baseline_id(
    primary_field: str,
    tail_enabled: bool,
    impact_enabled: bool,
    survivorship_enabled: bool,
    execution_policy_version: str = "",
    outcome_policy_version: str = "",
) -> str:
    parts = [
        "primary_{}".format(primary_field),
        "liquidity_cost",
        "tail" if tail_enabled else "no_tail",
        "impact" if impact_enabled else "no_impact",
        "survivorship" if survivorship_enabled else "no_survivorship",
    ]
    if execution_policy_version:
        parts.append("policy_{}".format(str(execution_policy_version).rsplit(".", 1)[-1]))
    if outcome_policy_version:
        parts.append("outcome_{}".format(str(outcome_policy_version).rsplit(".", 1)[-1]))
    return "validation_{}".format("__".join(parts))


def legacy_validation_baseline_id(strategy_name: str = "") -> str:
    primary_field, _, _ = primary_return_config(strategy_name)
    return build_validation_baseline_id(
        primary_field,
        tail_enabled=False,
        impact_enabled=False,
        survivorship_enabled=False,
    )


def stored_validation_baseline_id(stored_baseline_id: object, strategy_name: str = "") -> str:
    baseline_id = str(stored_baseline_id or "").strip()
    if baseline_id:
        return baseline_id
    return legacy_validation_baseline_id(strategy_name)


def matches_current_validation_baseline(
    stored_baseline_id: object,
    strategy_name: str = "",
    current_baseline_id: str = "",
) -> bool:
    expected = current_baseline_id or str(validation_baseline_config(strategy_name).get("baseline_id") or "")
    stored = stored_validation_baseline_id(stored_baseline_id, strategy_name)
    if stored == expected:
        return True
    stored_outcome = validation_baseline_outcome_fingerprint(stored)
    expected_outcome = validation_baseline_outcome_fingerprint(expected)
    return bool(stored_outcome and stored_outcome == expected_outcome)


def validation_baseline_outcome_fingerprint(baseline_id: object) -> str:
    return _baseline_part(str(baseline_id or ""), "outcome_")


def validation_baseline_config(
    strategy_name: str = "",
    execution_policy: Dict[str, object] = None,
) -> Dict[str, object]:
    primary_field, primary_days, primary_label = primary_return_config(strategy_name)
    execution_policy = execution_policy or build_execution_policy(strategy_name)
    frozen_cost = execution_policy.get("cost") or {}
    tail_enabled = bool((frozen_cost.get("tail_auction") or {}).get("enabled"))
    impact_enabled = bool((frozen_cost.get("market_impact") or {}).get("enabled"))
    slippage_enabled = any(
        coerce_number(value) > 0
        for value in (frozen_cost.get("liquidity_slippage_pct") or {}).values()
    )
    survivorship_enabled = bool(
        (execution_policy.get("delisting") or {}).get("after_entry") == "last_tradable_price"
    )
    outcome_policy_version = validation_outcome_policy_version(execution_policy)
    return {
        "baseline_id": build_validation_baseline_id(
            primary_field,
            tail_enabled=tail_enabled,
            impact_enabled=impact_enabled,
            survivorship_enabled=survivorship_enabled,
            execution_policy_version=str(execution_policy.get("policy_version") or ""),
            outcome_policy_version=outcome_policy_version,
        ),
        "strategy_name": str(strategy_name or ""),
        "primary_return_field": primary_field,
        "primary_holding_days": primary_days,
        "primary_horizon_label": primary_label,
        "net_return_formula": "{} - trade_cost_pct".format(primary_field),
        "cost_model": {
            "base_trade_cost_pct": coerce_number(
                frozen_cost.get("fee_round_trip_pct"),
                getattr(config, "VALIDATION_TRADE_COST_PCT", 0.25),
            ),
            "liquidity_slippage_enabled": slippage_enabled,
            "tail_auction_slippage_enabled": tail_enabled,
            "market_impact_enabled": impact_enabled,
            "portfolio_capital": coerce_number(
                (execution_policy.get("portfolio") or {}).get("capital"),
                getattr(config, "VALIDATION_PORTFOLIO_CAPITAL", 1_000_000),
            ),
            "default_position_pct": coerce_number(
                (execution_policy.get("portfolio") or {}).get("default_target_weight_pct"),
                getattr(config, "VALIDATION_DEFAULT_POSITION_PCT", 10.0),
            ),
        },
        "survivorship": {
            "enabled": survivorship_enabled,
            "stale_days": int(coerce_number(getattr(config, "SURVIVORSHIP_CORRECTION_STALE_DAYS", 30), 30)),
            "delisted_exit": "last_tradable_price",
            "missing_data_status": "unknown",
            "synthetic_default_return": False,
        },
        "execution_policy_version": execution_policy.get("policy_version"),
        "outcome_policy_version": outcome_policy_version,
        "execution_policy": execution_policy,
        "separate_legacy_baseline_required": True,
    }


def validation_outcome_policy_version(execution_policy: Dict[str, object]) -> str:
    policy = execution_policy if isinstance(execution_policy, dict) else {}
    entry = dict(policy.get("entry") or {})
    entry.pop("high_open_skip_pct", None)
    outcome_policy = {
        "strategy_name": str(policy.get("strategy_name") or ""),
        "entry": entry,
        "exit": policy.get("exit") or {},
        "price_limits": policy.get("price_limits") or {},
        "cost": policy.get("cost") or {},
        "portfolio": policy.get("portfolio") or {},
        "missing_or_unfilled": policy.get("missing_or_unfilled") or {},
        "delisting": policy.get("delisting") or {},
    }
    canonical = json.dumps(outcome_policy, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def _baseline_part(baseline_id: str, prefix: str) -> str:
    for part in str(baseline_id or "").split("__"):
        if part.startswith(prefix):
            return part[len(prefix) :]
    return ""


def strategy_exit_policy(strategy_name: str, holding_days: int, limit_down_pct: float) -> Dict[str, object]:
    policy = {"holding_days": holding_days, "limit_down_pct": limit_down_pct}
    if strategy_name not in {"tomorrow_picks", "swing_picks"}:
        return policy
    prefix = "TOMORROW_AUXILIARY" if strategy_name == "tomorrow_picks" else "SWING_VALIDATION"
    policy.update(
        {
            "take_profit_pct": max(
                0.0,
                coerce_number(getattr(config, "{}_TAKE_PROFIT_PCT".format(prefix), 8.0), 8.0),
            ),
            "stop_loss_pct": max(
                0.0,
                coerce_number(getattr(config, "{}_STOP_LOSS_PCT".format(prefix), 5.0), 5.0),
            ),
            "trailing_stop_pct": max(
                0.0,
                coerce_number(getattr(config, "{}_TRAILING_STOP_PCT".format(prefix), 4.0), 4.0),
            ),
        }
    )
    return policy


def exit_holding_days(strategy_name: str) -> int:
    if strategy_name == "tomorrow_picks":
        return 1
    if strategy_name == "swing_picks":
        return 5
    return primary_return_config(strategy_name)[1]


def outcome_ready(row, holding_days: int) -> bool:
    if bool(mapping_get(row, "survivorship_corrected", False)):
        return True
    future_days = int(mapping_get(row, "future_days", 1) or 1)
    if future_days >= max(1, int(holding_days or 1)):
        return True
    exit_reason = str(mapping_get(row, "exit_reason", "") or "")
    exit_days = int(mapping_get(row, "exit_days", 0) or 0)
    return exit_reason not in {"", "hold_to_term"} and 0 < exit_days <= future_days


def increment_reason(counter: Dict[str, int], reason: str) -> None:
    key = str(reason or "unknown").strip() or "unknown"
    counter[key] = counter.get(key, 0) + 1


def is_replay_version(strategy_version: str) -> bool:
    return "replay" in str(strategy_version or "").lower()


def mapping_get(row, key: str, default=None):
    if hasattr(row, "get"):
        return row.get(key, default)
    try:
        return row[key]
    except Exception:
        return default


def daily_limit_pct(code: str, market: str = "") -> float:
    normalized = normalize_code(code)
    market_text = str(market or "").lower()
    if normalized.startswith(("300", "301", "688")) or "创业" in market_text or "科创" in market_text:
        return 20.0
    return 10.0


def is_unbuyable_limit_up(row, previous_close: float, limit_pct: float) -> bool:
    previous = coerce_number(previous_close)
    if previous <= 0:
        return False
    open_price = coerce_number(row.get("open")) or coerce_number(row.get("price"))
    high = coerce_number(row.get("high")) or coerce_number(row.get("price"))
    low = coerce_number(row.get("low")) or coerce_number(row.get("price"))
    close = coerce_number(row.get("price")) or coerce_number(row.get("close"))
    if min(open_price, high, low, close) <= 0:
        return False
    limit_price = previous * (1 + max(1.0, coerce_number(limit_pct, 10.0)) / 100.0)
    return (
        open_price >= limit_price * 0.995
        and low >= limit_price * 0.995
        and high <= limit_price * 1.01
        and close >= limit_price * 0.995
    )


def liquidity_slippage_pct(turnover: float) -> float:
    amount = coerce_number(turnover)
    if amount >= 1_000_000_000:
        return coerce_number(getattr(config, "VALIDATION_SLIPPAGE_HIGH_TURNOVER_PCT", 0.05), 0.05)
    if amount >= 300_000_000:
        return coerce_number(getattr(config, "VALIDATION_SLIPPAGE_MID_TURNOVER_PCT", 0.12), 0.12)
    if amount >= 100_000_000:
        return coerce_number(getattr(config, "VALIDATION_SLIPPAGE_LOW_TURNOVER_PCT", 0.25), 0.25)
    return coerce_number(getattr(config, "VALIDATION_SLIPPAGE_MICRO_TURNOVER_PCT", 0.45), 0.45)


def estimated_order_amount(row) -> float:
    return _policy_estimated_order_amount(row, policy_from_signal(row))


def tail_auction_slippage_pct(row, base_slippage: float = 0.0) -> float:
    return _policy_tail_auction_slippage_pct(row, base_slippage, policy_from_signal(row))


def market_impact_cost_pct(row) -> float:
    return _policy_market_impact_cost_pct(row, policy_from_signal(row))


def execution_cost_pct(row) -> float:
    return _policy_execution_cost_components(row, policy_from_signal(row))["total_pct"]


def stored_or_current_trade_cost_pct(row) -> float:
    stored = coerce_number(mapping_get(row, "stored_trade_cost_pct"), None)
    if stored is None:
        stored = coerce_number(mapping_get(row, "trade_cost_pct"), None)
    if stored is not None:
        return round(stored, 4)
    return execution_cost_pct(row)


def is_primary_tomorrow_signal(rank, raw: Dict[str, object]) -> bool:
    if not isinstance(raw, dict):
        raw = {}
    tier = str(raw.get("tier") or "").strip()
    if tier:
        return tier == "primary_watch"
    return int(coerce_number(rank)) <= int(getattr(config, "TOMORROW_PRIMARY_WATCH_N", 10))


def is_primary_validation_signal(strategy_name: str, rank, raw: Dict[str, object]) -> bool:
    if not isinstance(raw, dict):
        raw = {}
    # 今天策略是非交易观察，但仍需要作为“信号至收盘”的主验证样本。
    if strategy_name == "short_term":
        return True
    if raw.get("execution_allowed") is False:
        return False
    tier = str(raw.get("tier") or "").strip()
    if tier:
        return tier == "primary_watch"
    if strategy_name == "tomorrow_picks":
        return is_primary_tomorrow_signal(rank, raw)
    return True
