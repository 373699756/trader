from typing import Dict, List

from . import config
from .normalization import coerce_number


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
    theme_limit = max(single, min(1.0, coerce_number(theme_cap, getattr(config, "PORTFOLIO_THEME_CAP", 0.35))))
    input_rows = [dict(row) for row in (rows or [])]
    eligible_rows = _eligible_portfolio_rows(input_rows)
    selected = eligible_rows[:max_pos]
    if not selected:
        return {
            "rows": [],
            "exposure": {},
            "summary": {
                "position_count": 0,
                "total_weight": 0.0,
                "cash_pct": 100.0,
                "input_count": len(input_rows),
                "excluded_count": len(input_rows) - len(eligible_rows),
                "no_trade_reason": _portfolio_no_trade_reason(input_rows),
            },
        }

    raw_weights = [_raw_weight(row) for row in selected]
    weights = _project_weights(selected, raw_weights, single, theme_limit)
    constraints_feasible = sum(weights) >= 0.999
    gross = _gross_exposure(market_regime or {}, performance or {})
    weights = [weight * gross["gross_exposure"] for weight in weights]
    total = sum(weights)

    exposure: Dict[str, float] = {}
    for row, weight in zip(selected, weights):
        theme = _theme_key(row)
        exposure[theme] = exposure.get(theme, 0.0) + weight
        row["portfolio_theme"] = theme
        row["suggested_weight"] = round(weight * 100, 2)
    return {
        "rows": selected,
        "exposure": {key: round(value * 100, 2) for key, value in sorted(exposure.items(), key=lambda item: item[1], reverse=True)},
        "summary": {
            "position_count": len(selected),
            "total_weight": round(total * 100, 2),
            "cash_pct": round(max(0.0, 1.0 - total) * 100, 2),
            "input_count": len(input_rows),
            "excluded_count": len(input_rows) - len(eligible_rows),
            "gross_exposure_pct": round(gross["gross_exposure"] * 100, 2),
            "regime_factor": round(gross["regime_factor"], 4),
            "drawdown_factor": round(gross["drawdown_factor"], 4),
            "gross_reasons": gross["reasons"],
            "single_cap_pct": round(single * 100, 2),
            "theme_cap_pct": round(theme_limit * 100, 2),
            "constraints_feasible": constraints_feasible,
        },
    }


def _eligible_portfolio_rows(rows: List[Dict[str, object]]) -> List[Dict[str, object]]:
    rows = [row for row in rows if row.get("execution_allowed") is not False]
    has_tier = any(str(row.get("tier") or "").strip() for row in rows)
    if not has_tier:
        return rows
    return [row for row in rows if str(row.get("tier") or "").strip() == "primary_watch"]


def _portfolio_no_trade_reason(rows: List[Dict[str, object]]) -> str:
    if not rows:
        return "暂无保存快照，请等待后台自动保存后再生成组合。"
    if any(str(row.get("tier") or "").strip() for row in rows):
        return "最近快照没有重点观察标的，备选观察不参与组合分仓。"
    return "最近快照没有可配置仓位的标的。"


def _raw_weight(row: Dict[str, object]) -> float:
    profile = row.get("serenity_profile") or {}
    confidence = coerce_number(profile.get("confidence_score"), coerce_number(row.get("confidence_score"), 50.0))
    risk = max(5.0, coerce_number(profile.get("risk_score"), coerce_number(row.get("risk_score"), 50.0)))
    volatility = coerce_number(row.get("volatility_20d")) or coerce_number(row.get("amplitude"))
    vol_penalty = max(1.0, volatility / 5.0) if volatility > 0 else 1.0
    return max(0.01, confidence / risk / vol_penalty)


def _theme_key(row: Dict[str, object]) -> str:
    return str(row.get("theme") or row.get("industry") or row.get("market_label") or "未分类")


def _gross_exposure(market_regime: Dict[str, object], performance: Dict[str, object]) -> Dict[str, object]:
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
    gross = _clamp01(regime_factor * drawdown_factor)
    return {
        "gross_exposure": gross,
        "regime_factor": regime_factor,
        "drawdown_factor": drawdown_factor,
        "reasons": [regime_reason, drawdown_reason],
    }


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, coerce_number(value)))


def _project_weights(
    rows: List[Dict[str, object]],
    raw_weights: List[float],
    single_cap: float,
    theme_cap: float,
) -> List[float]:
    total_raw = sum(raw_weights) or 1.0
    original = [max(0.0, value / total_raw) for value in raw_weights]
    weights = _enforce_caps(rows, original, single_cap, theme_cap)
    for _ in range(32):
        total = sum(weights)
        if total >= 1.0 - 1e-9:
            break
        rooms = _remaining_rooms(rows, weights, single_cap, theme_cap)
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
        weights = _enforce_caps(rows, next_weights, single_cap, theme_cap)
    return weights


def _enforce_caps(
    rows: List[Dict[str, object]],
    weights: List[float],
    single_cap: float,
    theme_cap: float,
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
        if not changed:
            break
    return [min(max(0.0, value), single_cap) for value in adjusted]


def _remaining_rooms(
    rows: List[Dict[str, object]],
    weights: List[float],
    single_cap: float,
    theme_cap: float,
) -> List[tuple]:
    exposure = _theme_exposure(rows, weights)
    rooms = []
    for idx, row in enumerate(rows):
        theme = _theme_key(row)
        room = min(single_cap - weights[idx], theme_cap - exposure.get(theme, 0.0))
        if room > 1e-9:
            rooms.append((idx, room))
    return rooms


def _theme_exposure(rows: List[Dict[str, object]], weights: List[float]) -> Dict[str, float]:
    exposure: Dict[str, float] = {}
    for row, weight in zip(rows, weights):
        theme = _theme_key(row)
        exposure[theme] = exposure.get(theme, 0.0) + weight
    return exposure
