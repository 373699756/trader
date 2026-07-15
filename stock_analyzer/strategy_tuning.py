from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Dict, List

from . import config
from .normalization import coerce_number
from .scoring_core.weights import WEIGHTS


SUPPORTED_TUNING_STRATEGIES = ("today_term", "tomorrow_picks", "swing_picks")
TUNING_PLAN_VERSION = "strategy_tuning_plan_v1"

_STRATEGY_LABELS = {
    "today_term": "今早执行推荐",
    "tomorrow_picks": "明日优先",
    "swing_picks": "2-5日持有",
}


def build_strategy_tuning_plan(
    strategy_name: str,
    metrics: Dict[str, object],
    dates: List[Dict[str, object]],
    days: int = 20,
) -> Dict[str, object]:
    if strategy_name not in SUPPORTED_TUNING_STRATEGIES:
        return {
            "ok": False,
            "strategy": strategy_name,
            "status": "unsupported_strategy",
            "error": "unsupported_strategy",
        }

    day_count = int(coerce_number(metrics.get("day_count"), 0))
    sample_count = int(coerce_number(metrics.get("sample_count"), 0))
    outcome_sample_count = int(coerce_number(metrics.get("outcome_sample_count"), 0))
    total_sample_count = int(coerce_number(metrics.get("total_sample_count"), sample_count))
    total_outcome_sample_count = int(
        coerce_number(metrics.get("total_outcome_sample_count"), outcome_sample_count)
    )
    real_sample_count = int(coerce_number(metrics.get("real_sample_count"), 0))
    replay_sample_count = int(coerce_number(metrics.get("replay_sample_count"), 0))
    real_outcome_sample_count = int(coerce_number(metrics.get("real_outcome_sample_count"), 0))
    replay_outcome_sample_count = int(coerce_number(metrics.get("replay_outcome_sample_count"), 0))
    real_count = int(coerce_number(metrics.get("real_day_count"), 0))
    replay_count = int(coerce_number(metrics.get("replay_day_count"), 0))
    pending_count = int(coerce_number(metrics.get("pending_outcome_count"), 0))
    unknown_count = int(coerce_number(metrics.get("unknown_outcome_count"), 0))
    win_rate = _first_number(metrics, "real_win_rate_primary_net", "win_rate_primary_net")
    avg_return = _first_number(metrics, "real_avg_primary_return_net", "avg_primary_return_net")
    drawdown = _first_number(
        metrics,
        "real_portfolio_max_drawdown_pct",
        "real_avg_max_drawdown_primary",
        "avg_max_drawdown_primary",
    )
    avg_return_ci_low = _first_number(metrics, "real_avg_primary_return_net_ci95_low")
    latest = dates[0] if dates else {}
    latest_count = int(coerce_number(latest.get("count"), 0))
    current_weights = dict(WEIGHTS.get(strategy_name, {}))

    issues: List[str] = []
    suggestions: List[Dict[str, object]] = []
    gates: List[Dict[str, object]] = []

    if day_count < 30:
        gates.append(_gate("min_day_count", False, day_count, 30, "有效交易日不足，不能自动应用。"))
        issues.append("有效交易日少，先影子运行，不直接改正式策略。")
    else:
        gates.append(_gate("min_day_count", True, day_count, 30, "有效交易日达到最低门槛。"))

    min_real_days = int(getattr(config, "STRATEGY_DECAY_MIN_REAL_DAYS", 20))
    if real_count < min_real_days:
        gates.append(_gate("min_real_day_count", False, real_count, min_real_days, "真实前瞻交易日不足。"))
        issues.append("真实样本不足，回放只能做粗筛。")
    else:
        gates.append(_gate("min_real_day_count", True, real_count, min_real_days, "真实交易日达到最低门槛。"))

    if pending_count > 0:
        gates.append(_gate("no_pending_outcomes", False, pending_count, 0, "仍有样本待回填，先不应用。"))
        issues.append("存在待回填样本，统计结论暂不稳定。")
    else:
        gates.append(_gate("no_pending_outcomes", True, 0, 0, "无待回填样本。"))

    if unknown_count > 0:
        gates.append(_gate("no_unknown_outcomes", False, unknown_count, 0, "存在数据状态未知样本，禁止晋级。"))
        issues.append("存在无法确认成交或退市状态的样本，必须修复数据后再评估。")
    else:
        gates.append(_gate("no_unknown_outcomes", True, 0, 0, "无数据状态未知样本。"))

    if win_rate is None:
        issues.append("胜率尚未形成有效统计。")
    elif win_rate < 45:
        issues.append("真实净胜率偏弱，需要提高过滤强度。")
    elif win_rate < 52:
        issues.append("真实净胜率一般，只适合小幅调参影子验证。")

    if avg_return is None:
        issues.append("平均净收益尚未形成有效统计。")
    elif avg_return <= 0:
        issues.append("平均净收益为负，应优先减少追高和过热样本。")
    gates.append(_gate("positive_avg_net_return", avg_return is not None and avg_return > 0, avg_return, "> 0", "主周期平均净收益必须为正。"))
    if bool(getattr(config, "STRATEGY_VALIDATION_REQUIRE_POSITIVE_CI", True)):
        gates.append(
            _gate(
                "positive_avg_net_return_ci95_low",
                avg_return_ci_low is not None and avg_return_ci_low > 0,
                avg_return_ci_low,
                "> 0",
                "主周期日组合净收益的95%置信下界必须为正。",
            )
        )

    min_win_rate = coerce_number(getattr(config, "STRATEGY_VALIDATION_MIN_WIN_RATE", 50.0), 50.0)
    gates.append(_gate("min_net_win_rate", win_rate is not None and win_rate >= min_win_rate, win_rate, min_win_rate, "真实交易日净胜率必须达标。"))
    drawdown_floor = coerce_number(
        getattr(config, "STRATEGY_VALIDATION_MAX_AVG_DRAWDOWN_PCT", -8.0),
        -8.0,
    )
    gates.append(_gate("max_primary_drawdown", drawdown is not None and drawdown > drawdown_floor, drawdown, "> {}".format(drawdown_floor), "真实日组合最大回撤不得突破硬限制。"))

    if latest and latest_count == 0:
        issues.append("最新批次为空，说明当前门槛下没有合格标的；空推荐本身可以保留。")

    suggestions.extend(_strategy_suggestions(strategy_name, win_rate, avg_return, latest_count))

    gate_passed = all(item["passed"] for item in gates)
    can_apply = False
    shadow_mode = True
    status = "shadow_only"
    reason = "调参建议只进入影子验证；正式晋级必须走冻结版本的Meta/OOS门控。"
    if not gate_passed:
        status = "blocked"
        reason = "未通过自动应用门控，只保存为调参建议。"
    if strategy_name == "today_term":
        status = "shadow_only"
        shadow_mode = True
        can_apply = False
        reason = "今早策略按信号后可执行样本评估，参数建议先留给调参观察，不直接变更生产执行参数。"

    plan = {
        "ok": True,
        "plan_version": TUNING_PLAN_VERSION,
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
            "day_count": day_count,
            "sample_count": sample_count,
            "outcome_sample_count": outcome_sample_count,
            "total_sample_count": total_sample_count,
            "total_outcome_sample_count": total_outcome_sample_count,
            "real_sample_count": real_sample_count,
            "replay_sample_count": replay_sample_count,
            "real_outcome_sample_count": real_outcome_sample_count,
            "replay_outcome_sample_count": replay_outcome_sample_count,
            "real_day_count": real_count,
            "replay_day_count": replay_count,
            "pending_outcome_count": pending_count,
            "unknown_outcome_count": unknown_count,
            "win_rate_primary_net": win_rate,
            "avg_primary_return_net": avg_return,
            "avg_primary_return_net_ci95_low": avg_return_ci_low,
            "avg_max_drawdown_primary": drawdown,
            "latest_signal_date": latest.get("signal_date", ""),
            "latest_signal_count": latest_count,
        },
        "current_weights": current_weights,
    }
    plan["input_fingerprint"] = _tuning_plan_fingerprint(plan)
    return plan


def _tuning_plan_fingerprint(plan: Dict[str, object]) -> str:
    semantic_plan = {
        key: value
        for key, value in plan.items()
        if key not in {"generated_at", "input_fingerprint"}
    }
    payload = json.dumps(
        semantic_plan,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _strategy_suggestions(strategy_name: str, win_rate, avg_return, latest_count: int) -> List[Dict[str, object]]:
    weak = (win_rate is not None and win_rate < 50) or (avg_return is not None and avg_return < 0)
    empty = latest_count == 0
    if strategy_name == "tomorrow_picks":
        if weak:
            return [
                _suggest("min_score", "+2", "提高明日优先最低分，减少弱承接样本。"),
                _suggest("risk_penalty_multiplier", "1.15", "放大追高、过热、回落风险惩罚。"),
                _suggest("theme_cap", "2", "限制单行业集中，避免单一题材误伤。"),
            ]
        if empty:
            return [_suggest("shadow_min_score", "-1", "仅影子验证轻微放宽门槛，正式策略继续允许空推荐。")]
        return [_suggest("keep", "no_change", "当前先保持参数，继续积累真实样本。")]
    if strategy_name == "today_term":
        if weak:
            return [
                _suggest("execution_quality", "+0.10", "今早策略若验证偏弱，可适度提高趋势、承接和尾盘执行质量权重。"),
                _suggest("overheat_penalty", "+10%", "降低高涨幅、高换手、高量比样本权重，减少回落和流动性风险。"),
            ]
        if empty:
            return [_suggest("shadow_min_score", "-1", "仅在调参观察内轻微放宽今早门槛。")]
        return [_suggest("keep", "no_change", "当前先保持参数，继续积累今早执行口径的真实样本。")]
    if strategy_name == "swing_picks":
        if weak:
            return [
                _suggest("not_overextended_weight", "+0.04", "2-5天策略加强不过热因子。"),
                _suggest("momentum_weight", "-0.04", "降低单纯动量追高权重。"),
                _suggest("theme_cap", "3", "控制行业集中度。"),
            ]
        return [_suggest("keep", "no_change", "当前先保持参数，等待5日样本成熟。")]
    return []


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
