from typing import Dict, List

from . import config
from .normalization import coerce_number
from .strategy_validation import market_impact_cost_pct


def build_portfolio(
    rows: List[Dict[str, object]],
    max_positions: int = None,
    single_cap: float = None,
    theme_cap: float = None,
    market_regime: Dict[str, object] = None,
    performance: Dict[str, object] = None,
) -> Dict[str, object]:
    max_pos = max(1, int(max_positions or getattr(config, "PORTFOLIO_MAX_POSITIONS", 10)))
    single = max(0.01, min(1.0, coerce_number(single_cap, getattr(config, "PORTFOLIO_SINGLE_CAP", 0.15))))
    base_theme_limit = max(single, min(1.0, coerce_number(theme_cap, getattr(config, "PORTFOLIO_THEME_CAP", 0.35))))
    theme_limit = _regime_aware_theme_cap(base_theme_limit, market_regime or {}, single)
    correlation_limit = _correlation_group_cap(single, theme_limit)
    input_rows = [dict(row) for row in (rows or [])]
    eligible_rows = _eligible_portfolio_rows(input_rows)
    gross = _gross_exposure(market_regime or {}, performance or {}, eligible_rows)
    selected, capacity_excluded, weights = _select_capacity_checked_rows(
        eligible_rows,
        max_pos,
        single,
        theme_limit,
        correlation_limit,
        gross["gross_exposure"],
    )
    if not selected:
        return {
            "rows": [],
            "exposure": {},
            "summary": {
                "position_count": 0,
                "total_weight": 0.0,
                "cash_pct": 100.0,
                "input_count": len(input_rows),
                "excluded_count": len(input_rows) - len(eligible_rows) + len(capacity_excluded),
                "capacity_excluded_count": len(capacity_excluded),
                "capacity_ok": len(capacity_excluded) == 0,
                "capacity_overflow": _capacity_overflow_summary(capacity_excluded),
                "no_trade_reason": _portfolio_no_trade_reason(input_rows, capacity_excluded),
                "gross_exposure_pct": round(gross["gross_exposure"] * 100, 2),
                "regime_factor": round(gross["regime_factor"], 4),
                "drawdown_factor": round(gross["drawdown_factor"], 4),
                "volatility_factor": round(gross["volatility_factor"], 4),
                "portfolio_volatility_pct": gross["portfolio_volatility_pct"],
                "target_volatility_pct": gross["target_volatility_pct"],
                "gross_reasons": gross["reasons"],
                "portfolio_optimization_enabled": bool(getattr(config, "ENABLE_PORTFOLIO_OPTIMIZATION", True)),
                "correlation_group_cap_pct": round(correlation_limit * 100, 2),
                "correlation_exposure": {},
            },
        }

    constraints_feasible = sum(weights) >= 0.999
    total = sum(weights)

    exposure: Dict[str, float] = {}
    correlation_exposure: Dict[str, float] = {}
    for row, weight in zip(selected, weights):
        theme = _theme_key(row)
        exposure[theme] = exposure.get(theme, 0.0) + weight
        corr_group = _correlation_group_key(row)
        correlation_exposure[corr_group] = correlation_exposure.get(corr_group, 0.0) + weight
        row["portfolio_theme"] = theme
        row["portfolio_correlation_group"] = corr_group
        row["suggested_weight"] = round(weight * 100, 2)
    return {
        "rows": selected,
        "exposure": {key: round(value * 100, 2) for key, value in sorted(exposure.items(), key=lambda item: item[1], reverse=True)},
        "summary": {
            "position_count": len(selected),
            "total_weight": round(total * 100, 2),
            "cash_pct": round(max(0.0, 1.0 - total) * 100, 2),
            "input_count": len(input_rows),
            "excluded_count": len(input_rows) - len(eligible_rows) + len(capacity_excluded),
            "capacity_excluded_count": len(capacity_excluded),
            "capacity_ok": len(capacity_excluded) == 0,
            "capacity_overflow": _capacity_overflow_summary(capacity_excluded),
            "gross_exposure_pct": round(gross["gross_exposure"] * 100, 2),
            "regime_factor": round(gross["regime_factor"], 4),
            "drawdown_factor": round(gross["drawdown_factor"], 4),
            "volatility_factor": round(gross["volatility_factor"], 4),
            "portfolio_volatility_pct": gross["portfolio_volatility_pct"],
            "target_volatility_pct": gross["target_volatility_pct"],
            "gross_reasons": gross["reasons"],
            "single_cap_pct": round(single * 100, 2),
            "theme_cap_pct": round(theme_limit * 100, 2),
            "base_theme_cap_pct": round(base_theme_limit * 100, 2),
            "correlation_group_cap_pct": round(correlation_limit * 100, 2),
            "correlation_exposure": {
                key: round(value * 100, 2)
                for key, value in sorted(correlation_exposure.items(), key=lambda item: item[1], reverse=True)
            },
            "portfolio_optimization_enabled": bool(getattr(config, "ENABLE_PORTFOLIO_OPTIMIZATION", True)),
            "constraints_feasible": constraints_feasible,
        },
    }


def _eligible_portfolio_rows(rows: List[Dict[str, object]]) -> List[Dict[str, object]]:
    rows = [row for row in rows if row.get("execution_allowed") is not False]
    has_tier = any(str(row.get("tier") or "").strip() for row in rows)
    if not has_tier:
        return rows
    return [row for row in rows if str(row.get("tier") or "").strip() == "primary_watch"]


def _portfolio_no_trade_reason(rows: List[Dict[str, object]], capacity_excluded: List[Dict[str, object]] = None) -> str:
    if capacity_excluded:
        return "候选标的市场冲击超限，当前资金规模下不建议分仓。"
    if not rows:
        return "暂无保存快照，请等待后台自动保存后再生成组合。"
    if any(str(row.get("tier") or "").strip() for row in rows):
        return "最近快照没有重点观察标的，备选观察不参与组合分仓。"
    return "最近快照没有可配置仓位的标的。"


def _select_capacity_checked_rows(
    rows: List[Dict[str, object]],
    max_positions: int,
    single_cap: float,
    theme_cap: float,
    correlation_cap: float,
    gross_exposure: float,
) -> tuple:
    if not bool(getattr(config, "ENABLE_MARKET_IMPACT", False)):
        selected = rows[:max_positions]
        return selected, [], _portfolio_weights(selected, single_cap, theme_cap, correlation_cap, gross_exposure)
    candidates = list(rows)
    excluded: List[Dict[str, object]] = []
    for _ in range(len(rows) + 1):
        selected = candidates[:max_positions]
        selected, overflow, weights = _apply_market_impact_capacity_filter(
            selected,
            single_cap,
            theme_cap,
            correlation_cap,
            gross_exposure,
        )
        if not overflow:
            return selected, excluded, weights
        excluded.extend(overflow)
        overflow_keys = {_capacity_row_key(row) for row in overflow}
        candidates = [row for row in candidates if _capacity_row_key(row) not in overflow_keys]
        if not candidates:
            return [], excluded, []
    selected = candidates[:max_positions]
    return selected, excluded, _portfolio_weights(selected, single_cap, theme_cap, correlation_cap, gross_exposure)


def _apply_market_impact_capacity_filter(
    rows: List[Dict[str, object]],
    single_cap: float,
    theme_cap: float,
    correlation_cap: float,
    gross_exposure: float,
) -> tuple:
    if not bool(getattr(config, "ENABLE_MARKET_IMPACT", False)):
        return rows, [], _portfolio_weights(rows, single_cap, theme_cap, correlation_cap, gross_exposure)
    max_impact = max(0.0, coerce_number(getattr(config, "MAX_ACCEPTABLE_IMPACT_PCT", 1.0), 1.0))
    selected = list(rows)
    excluded: List[Dict[str, object]] = []
    for _ in range(len(rows) + 1):
        weights = _portfolio_weights(selected, single_cap, theme_cap, correlation_cap, gross_exposure)
        overflow = []
        kept = []
        for row, weight in zip(selected, weights):
            check_row = dict(row)
            check_row["suggested_weight"] = round(weight * 100.0, 4)
            impact = market_impact_cost_pct(check_row)
            row["impact_pct"] = impact
            row["market_impact_pct"] = impact
            if impact <= max_impact + 1e-12:
                kept.append(row)
            else:
                excluded_row = dict(row)
                excluded_row["impact_pct"] = impact
                excluded_row["market_impact_pct"] = impact
                excluded_row["reason"] = "市场冲击超限"
                overflow.append(excluded_row)
        if not overflow:
            return selected, excluded, weights
        excluded.extend(overflow)
        selected = kept
        if not selected:
            return [], excluded, []
    return selected, excluded, _portfolio_weights(selected, single_cap, theme_cap, correlation_cap, gross_exposure)


def _capacity_row_key(row: Dict[str, object]) -> str:
    return "{}|{}|{}".format(row.get("code") or "", row.get("rank") or "", row.get("name") or "")


def _portfolio_weights(
    rows: List[Dict[str, object]],
    single_cap: float,
    theme_cap: float,
    correlation_cap: float,
    gross_exposure: float,
) -> List[float]:
    if not rows:
        return []
    raw_weights = [_raw_weight(row) for row in rows]
    weights = _project_weights(rows, raw_weights, single_cap, theme_cap, correlation_cap)
    return [weight * gross_exposure for weight in weights]


def _capacity_overflow_summary(rows: List[Dict[str, object]]) -> List[Dict[str, object]]:
    return [
        {
            "code": row.get("code"),
            "name": row.get("name"),
            "impact_pct": round(coerce_number(row.get("impact_pct")), 4),
            "reason": row.get("reason") or "市场冲击超限",
        }
        for row in rows[:10]
    ]


def _raw_weight(row: Dict[str, object]) -> float:
    profile = row.get("serenity_profile") or {}
    confidence = coerce_number(profile.get("confidence_score"), coerce_number(row.get("confidence_score"), 50.0))
    risk = max(5.0, coerce_number(profile.get("risk_score"), coerce_number(row.get("risk_score"), 50.0)))
    volatility = coerce_number(row.get("volatility_20d")) or coerce_number(row.get("amplitude"))
    vol_penalty = max(1.0, volatility / 5.0) if volatility > 0 else 1.0
    base = confidence / risk / vol_penalty
    if bool(getattr(config, "ENABLE_PORTFOLIO_OPTIMIZATION", True)):
        base *= _expected_edge_multiplier(row)
    return max(0.01, base)


def _expected_edge_multiplier(row: Dict[str, object]) -> float:
    if not _expected_edge_allowed(row):
        return 1.0
    expected = coerce_number(row.get("expected_return_net"), None)
    probability = coerce_number(row.get("p_win"), None)
    if probability is not None and abs(probability) <= 1:
        probability *= 100.0
    multiplier = 1.0
    if expected is not None:
        multiplier *= max(0.6, min(1.5, 1.0 + expected / 8.0))
    if probability is not None:
        multiplier *= max(0.7, min(1.35, 1.0 + (probability - 50.0) / 100.0))
    return max(0.35, min(1.75, multiplier))


def _expected_edge_allowed(row: Dict[str, object]) -> bool:
    if str(row.get("model_confidence") or "").strip().lower() != "ready":
        return False
    if str(row.get("ranking_source") or "").strip() == "expected_return_predicted_net_return":
        return True
    return row.get("expected_return_rank") is not None


def _regime_aware_theme_cap(base_theme_cap: float, market_regime: Dict[str, object], single_cap: float) -> float:
    base = max(single_cap, min(1.0, coerce_number(base_theme_cap, getattr(config, "PORTFOLIO_THEME_CAP", 0.35))))
    if not bool(getattr(config, "ENABLE_REGIME_THEME_CAP", False)):
        return base
    level = str((market_regime or {}).get("level") or "").strip().lower()
    score = coerce_number((market_regime or {}).get("score"), None)
    if level == "risk_off" or (score is not None and score <= 42):
        multiplier = coerce_number(getattr(config, "PORTFOLIO_THEME_CAP_RISK_OFF_MULTIPLIER", 0.7), 0.7)
    elif level == "risk_on" or (score is not None and score >= 68):
        multiplier = coerce_number(getattr(config, "PORTFOLIO_THEME_CAP_RISK_ON_MULTIPLIER", 1.0), 1.0)
    else:
        multiplier = coerce_number(getattr(config, "PORTFOLIO_THEME_CAP_BALANCED_MULTIPLIER", 0.9), 0.9)
    return max(single_cap, min(1.0, base * max(0.01, multiplier)))


def _theme_key(row: Dict[str, object]) -> str:
    return str(row.get("theme") or row.get("industry") or row.get("market_label") or "未分类")


def _correlation_group_key(row: Dict[str, object]) -> str:
    for key in ("correlation_group", "risk_group", "industry", "theme", "market_label"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return "未分类"


def _correlation_group_cap(single_cap: float, theme_cap: float) -> float:
    if not bool(getattr(config, "ENABLE_PORTFOLIO_OPTIMIZATION", True)):
        return 1.0
    configured = coerce_number(getattr(config, "PORTFOLIO_CORRELATION_GROUP_CAP", 0.45), 0.45)
    return max(single_cap, min(1.0, configured, max(theme_cap, single_cap)))


def _gross_exposure(
    market_regime: Dict[str, object],
    performance: Dict[str, object],
    rows: List[Dict[str, object]] = None,
) -> Dict[str, object]:
    score = coerce_number((market_regime or {}).get("score"), 68.0)
    risk_on = _clamp01(getattr(config, "PORTFOLIO_GROSS_RISK_ON", 1.0))
    balanced = _clamp01(getattr(config, "PORTFOLIO_GROSS_BALANCED", 0.7))
    risk_off = _clamp01(getattr(config, "PORTFOLIO_GROSS_RISK_OFF", 0.4))
    if score >= 68:
        regime_factor = risk_on
        regime_reason = "偏进攻市况，总仓上限接近满仓"
    elif score <= 42:
        regime_factor = risk_off
        regime_reason = "偏防守市况，总仓降档"
    elif score >= 50:
        ratio = (score - 50.0) / 18.0
        regime_factor = balanced + (risk_on - balanced) * max(0.0, min(1.0, ratio))
        regime_reason = "均衡偏强，总仓温和提高"
    else:
        ratio = (score - 42.0) / 8.0
        regime_factor = risk_off + (balanced - risk_off) * max(0.0, min(1.0, ratio))
        regime_reason = "均衡偏弱，总仓保守"

    metrics = (performance or {}).get("metrics") if isinstance(performance, dict) else {}
    drawdown = abs(min(0.0, coerce_number((metrics or {}).get("max_drawdown_pct"))))
    level_2 = coerce_number(getattr(config, "PORTFOLIO_DD_LEVEL_2", 15.0), 15.0)
    level_1 = coerce_number(getattr(config, "PORTFOLIO_DD_LEVEL_1", 8.0), 8.0)
    if drawdown >= level_2:
        drawdown_factor = _clamp01(getattr(config, "PORTFOLIO_DD_FACTOR_2", 0.4))
        drawdown_reason = "组合回撤超过{}%，触发强降仓".format(level_2)
    elif drawdown >= level_1:
        drawdown_factor = _clamp01(getattr(config, "PORTFOLIO_DD_FACTOR_1", 0.7))
        drawdown_reason = "组合回撤超过{}%，触发降仓".format(level_1)
    else:
        drawdown_factor = 1.0
        drawdown_reason = "组合回撤未触发降仓"
    volatility = _portfolio_volatility_pct(rows or [])
    volatility_factor = _volatility_target_factor(volatility)
    if volatility is None:
        volatility_reason = "候选波动率不足，波动率目标化不调整"
    elif volatility_factor < 0.999:
        volatility_reason = "候选波动率{:.2f}%高于目标，组合降仓".format(volatility)
    elif volatility_factor > 1.001:
        volatility_reason = "候选波动率{:.2f}%低于目标，允许温和加仓".format(volatility)
    else:
        volatility_reason = "候选波动率接近目标，组合仓位不调整"
    gross = _clamp01(regime_factor * drawdown_factor * volatility_factor)
    return {
        "gross_exposure": gross,
        "regime_factor": regime_factor,
        "drawdown_factor": drawdown_factor,
        "volatility_factor": volatility_factor,
        "portfolio_volatility_pct": None if volatility is None else round(volatility, 4),
        "target_volatility_pct": coerce_number(getattr(config, "PORTFOLIO_TARGET_VOLATILITY_PCT", 5.0), 5.0),
        "reasons": [regime_reason, drawdown_reason, volatility_reason],
    }


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, coerce_number(value)))


def _portfolio_volatility_pct(rows: List[Dict[str, object]]):
    values = []
    for row in rows or []:
        value = coerce_number(row.get("volatility_20d"), None)
        if value is None or value <= 0:
            value = coerce_number(row.get("amplitude"), None)
        if value is not None and value > 0:
            values.append(value)
    if not values:
        return None
    return sum(values) / len(values)


def _volatility_target_factor(volatility_pct) -> float:
    if not bool(getattr(config, "ENABLE_VOLATILITY_TARGETING", True)):
        return 1.0
    volatility = coerce_number(volatility_pct, None)
    if volatility is None or volatility <= 0:
        return 1.0
    target = max(0.01, coerce_number(getattr(config, "PORTFOLIO_TARGET_VOLATILITY_PCT", 5.0), 5.0))
    min_scale = max(0.0, coerce_number(getattr(config, "PORTFOLIO_VOL_SCALE_MIN", 0.35), 0.35))
    max_scale = max(min_scale, coerce_number(getattr(config, "PORTFOLIO_VOL_SCALE_MAX", 1.15), 1.15))
    return max(min_scale, min(max_scale, target / volatility))


def _project_weights(
    rows: List[Dict[str, object]],
    raw_weights: List[float],
    single_cap: float,
    theme_cap: float,
    correlation_cap: float,
) -> List[float]:
    total_raw = sum(raw_weights) or 1.0
    original = [max(0.0, value / total_raw) for value in raw_weights]
    weights = _enforce_caps(rows, original, single_cap, theme_cap, correlation_cap)
    for _ in range(32):
        total = sum(weights)
        if total >= 1.0 - 1e-9:
            break
        rooms = _remaining_rooms(rows, weights, single_cap, theme_cap, correlation_cap)
        if not rooms:
            break
        spare = 1.0 - total
        base_total = sum(original[idx] for idx, _ in rooms)
        room_total = sum(room for _, room in rooms)
        if room_total <= 1e-12:
            break
        added = 0.0
        next_weights = list(weights)
        for idx, room in rooms:
            share = (original[idx] / base_total) if base_total > 1e-12 else (room / room_total)
            value = min(room, spare * share)
            next_weights[idx] += value
            added += value
        if added <= 1e-12:
            break
        weights = _enforce_caps(rows, next_weights, single_cap, theme_cap, correlation_cap)
    return weights


def _enforce_caps(
    rows: List[Dict[str, object]],
    weights: List[float],
    single_cap: float,
    theme_cap: float,
    correlation_cap: float,
) -> List[float]:
    adjusted = [min(max(0.0, value), single_cap) for value in weights]
    for _ in range(8):
        exposure = _theme_exposure(rows, adjusted)
        changed = False
        for theme, total in exposure.items():
            if total <= theme_cap + 1e-12:
                continue
            ratio = theme_cap / total
            for idx, row in enumerate(rows):
                if _theme_key(row) == theme:
                    adjusted[idx] *= ratio
                    changed = True
        if bool(getattr(config, "ENABLE_PORTFOLIO_OPTIMIZATION", True)):
            corr_exposure = _correlation_exposure(rows, adjusted)
            for group, total in corr_exposure.items():
                if total <= correlation_cap + 1e-12:
                    continue
                ratio = correlation_cap / total
                for idx, row in enumerate(rows):
                    if _correlation_group_key(row) == group:
                        adjusted[idx] *= ratio
                        changed = True
        if not changed:
            break
    return [min(max(0.0, value), single_cap) for value in adjusted]


def _remaining_rooms(
    rows: List[Dict[str, object]],
    weights: List[float],
    single_cap: float,
    theme_cap: float,
    correlation_cap: float,
) -> List[tuple]:
    exposure = _theme_exposure(rows, weights)
    corr_exposure = _correlation_exposure(rows, weights)
    rooms = []
    for idx, row in enumerate(rows):
        theme = _theme_key(row)
        corr_group = _correlation_group_key(row)
        room = min(
            single_cap - weights[idx],
            theme_cap - exposure.get(theme, 0.0),
            correlation_cap - corr_exposure.get(corr_group, 0.0),
        )
        if room > 1e-9:
            rooms.append((idx, room))
    return rooms


def _theme_exposure(rows: List[Dict[str, object]], weights: List[float]) -> Dict[str, float]:
    exposure: Dict[str, float] = {}
    for row, weight in zip(rows, weights):
        theme = _theme_key(row)
        exposure[theme] = exposure.get(theme, 0.0) + weight
    return exposure


def _correlation_exposure(rows: List[Dict[str, object]], weights: List[float]) -> Dict[str, float]:
    exposure: Dict[str, float] = {}
    for row, weight in zip(rows, weights):
        group = _correlation_group_key(row)
        exposure[group] = exposure.get(group, 0.0) + weight
    return exposure
