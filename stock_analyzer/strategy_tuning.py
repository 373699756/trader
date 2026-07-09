from __future__ import annotations

from datetime import datetime
from typing import Dict, List

from .normalization import coerce_number
from .scoring import WEIGHTS


SUPPORTED_TUNING_STRATEGIES = ("short_term", "tomorrow_picks", "swing_picks")

_STRATEGY_LABELS = {
    "short_term": "今天推荐",
    "tomorrow_picks": "明天推荐",
    "swing_picks": "2-5天推荐",
}


def build_strategy_tuning_plan(
    strategy_name: str,
    metrics: Dict[str, object],
    dates: List[Dict[str, object]],
    deepseek_review: Dict[str, object] | None = None,
    days: int = 20,
) -> Dict[str, object]:
    if strategy_name not in SUPPORTED_TUNING_STRATEGIES:
        return {
            "ok": False,
            "strategy": strategy_name,
            "status": "unsupported_strategy",
            "error": "unsupported_strategy",
        }

    sample_count = int(coerce_number(metrics.get("sample_count"), 0))
    real_count = int(coerce_number(metrics.get("real_sample_count"), 0))
    replay_count = int(coerce_number(metrics.get("replay_sample_count"), 0))
    pending_count = int(coerce_number(metrics.get("pending_outcome_count"), 0))
    win_rate = _first_number(metrics, "real_win_rate_primary_net", "win_rate_primary_net")
    avg_return = _first_number(metrics, "real_avg_primary_return_net", "avg_primary_return_net")
    latest = dates[0] if dates else {}
    latest_count = int(coerce_number(latest.get("count"), 0))
    current_weights = dict(WEIGHTS.get(strategy_name, {}))

    issues: List[str] = []
    suggestions: List[Dict[str, object]] = []
    gates: List[Dict[str, object]] = []

    if sample_count < 30:
        gates.append(_gate("min_sample_count", False, sample_count, 30, "有效样本不足，不能自动应用。"))
        issues.append("有效样本少，先影子运行，不直接改正式策略。")
    else:
        gates.append(_gate("min_sample_count", True, sample_count, 30, "有效样本达到最低门槛。"))

    if real_count < 10:
        gates.append(_gate("min_real_sample_count", False, real_count, 10, "真实前瞻样本不足。"))
        issues.append("真实样本不足，回放只能做粗筛。")
    else:
        gates.append(_gate("min_real_sample_count", True, real_count, 10, "真实样本达到最低门槛。"))

    if pending_count > 0:
        gates.append(_gate("no_pending_outcomes", False, pending_count, 0, "仍有样本待回填，先不应用。"))
        issues.append("存在待回填样本，统计结论暂不稳定。")
    else:
        gates.append(_gate("no_pending_outcomes", True, 0, 0, "无待回填样本。"))

    if win_rate is None:
        issues.append("胜率尚未形成有效统计。")
    elif win_rate < 45:
        issues.append("真实净胜率偏弱，需要提高过滤强度。")
    elif win_rate < 52:
        issues.append("真实净胜率一般，只适合小幅调参影子验证。")

    if avg_return is None:
        issues.append("平均净收益尚未形成有效统计。")
    elif avg_return < 0:
        issues.append("平均净收益为负，应优先减少追高和过热样本。")

    if latest and latest_count == 0:
        issues.append("最新批次为空，说明当前门槛下没有合格标的；空推荐本身可以保留。")

    suggestions.extend(_strategy_suggestions(strategy_name, win_rate, avg_return, latest_count))
    suggestions.extend(_deepseek_rule_suggestions(deepseek_review or {}))

    gate_passed = all(item["passed"] for item in gates)
    deepseek_upgrade = _deepseek_applicable_upgrade(deepseek_review or {})
    can_apply = False
    shadow_mode = True
    status = "shadow_only"
    reason = "建议先进入影子验证，不直接改正式策略。"
    if not gate_passed:
        status = "blocked"
        reason = "未通过自动应用门控，只保存为调参建议。"
    elif deepseek_upgrade:
        can_apply = True
        shadow_mode = False
        status = "ready_for_confirmation"
        reason = "DeepSeek 候选已通过 OOS 门槛，可进入人工确认采纳。"

    return {
        "ok": True,
        "strategy": strategy_name,
        "strategy_label": _STRATEGY_LABELS.get(strategy_name, strategy_name),
        "days": int(days),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "status": status,
        "can_apply": can_apply,
        "shadow_mode": shadow_mode,
        "reason": reason,
        "issues": _unique(issues),
        "suggestions": suggestions,
        "gate": {
            "passed": gate_passed,
            "can_apply": can_apply,
            "items": gates,
        },
        "metrics_snapshot": {
            "sample_count": sample_count,
            "real_sample_count": real_count,
            "replay_sample_count": replay_count,
            "pending_outcome_count": pending_count,
            "win_rate_primary_net": win_rate,
            "avg_primary_return_net": avg_return,
            "latest_signal_date": latest.get("signal_date", ""),
            "latest_signal_count": latest_count,
        },
        "current_weights": current_weights,
        "deepseek": _compact_deepseek(deepseek_review or {}),
    }


def _strategy_suggestions(strategy_name: str, win_rate, avg_return, latest_count: int) -> List[Dict[str, object]]:
    weak = (win_rate is not None and win_rate < 50) or (avg_return is not None and avg_return < 0)
    empty = latest_count == 0
    if strategy_name == "tomorrow_picks":
        if weak:
            return [
                _suggest("min_score", "+2", "提高明天推荐最低分，减少弱承接样本。"),
                _suggest("risk_penalty_multiplier", "1.15", "放大追高、过热、回落风险惩罚。"),
                _suggest("theme_cap", "2", "限制单行业集中，避免单一题材误伤。"),
            ]
        if empty:
            return [_suggest("shadow_min_score", "-1", "仅影子验证轻微放宽门槛，正式策略继续允许空推荐。")]
        return [_suggest("keep", "no_change", "当前先保持参数，继续积累真实样本。")]
    if strategy_name == "short_term":
        if weak:
            return [
                _suggest("reversal_tilt", "+0.10", "盘中强势若验证偏弱，影子增加短线反转修正。"),
                _suggest("overheat_penalty", "+10%", "降低高涨幅、高换手、高量比样本权重。"),
            ]
        if empty:
            return [_suggest("shadow_min_score", "-1", "仅影子验证轻微放宽今天推荐门槛。")]
        return [_suggest("keep", "no_change", "当前先保持参数，继续观察。")]
    if strategy_name == "swing_picks":
        if weak:
            return [
                _suggest("not_overextended_weight", "+0.04", "2-5天策略加强不过热因子。"),
                _suggest("momentum_weight", "-0.04", "降低单纯动量追高权重。"),
                _suggest("theme_cap", "3", "控制行业集中度。"),
            ]
        return [_suggest("keep", "no_change", "当前先保持参数，等待5日样本成熟。")]
    return []


def _deepseek_rule_suggestions(review: Dict[str, object]) -> List[Dict[str, object]]:
    if not review or review.get("enabled") is False:
        return []
    rules = review.get("rule_candidates") or []
    result = []
    for rule in rules[:4]:
        if not isinstance(rule, dict):
            continue
        field = str(rule.get("field") or "").strip()
        if not field:
            continue
        result.append(
            _suggest(
                "deepseek_rule",
                {
                    "field": field,
                    "operator": rule.get("operator"),
                    "threshold": rule.get("threshold"),
                    "penalty": rule.get("penalty"),
                    "can_apply": bool(rule.get("can_apply")),
                    "oos_improvement": (rule.get("oos_evaluation") or {}).get("oos_improvement"),
                    "positive_folds": (rule.get("oos_evaluation") or {}).get("positive_folds"),
                    "fold_count": (rule.get("oos_evaluation") or {}).get("fold_count"),
                },
                str(rule.get("reason") or "DeepSeek 建议进入影子规则验证。"),
                source="deepseek",
            )
        )
    return result


def _deepseek_applicable_upgrade(review: Dict[str, object]) -> bool:
    if not isinstance(review, dict):
        return False
    for rule in review.get("rule_candidates") or []:
        if isinstance(rule, dict) and rule.get("can_apply"):
            return True
    alpha = review.get("blend_alpha_calibration")
    if isinstance(alpha, dict) and alpha.get("can_apply"):
        return True
    return False


def _suggest(parameter: str, value, reason: str, source: str = "local") -> Dict[str, object]:
    return {"parameter": parameter, "value": value, "reason": reason, "source": source}


def _gate(name: str, passed: bool, actual, required, reason: str) -> Dict[str, object]:
    return {"name": name, "passed": bool(passed), "actual": actual, "required": required, "reason": reason}


def _first_number(payload: Dict[str, object], *keys):
    for key in keys:
        value = payload.get(key)
        if value is not None:
            return round(coerce_number(value), 4)
    return None


def _unique(values: List[str]) -> List[str]:
    result: List[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def _compact_deepseek(review: Dict[str, object]) -> Dict[str, object]:
    if not review:
        return {"enabled": False, "status": "not_requested"}
    return {
        "enabled": bool(review.get("enabled")),
        "status": review.get("status", ""),
        "source": review.get("source", ""),
        "decision": review.get("decision", ""),
        "summary": review.get("summary", ""),
        "avoid_conditions": review.get("avoid_conditions") or [],
        "rule_candidates": review.get("rule_candidates") or [],
    }
