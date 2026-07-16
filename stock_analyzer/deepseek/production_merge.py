from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, time

from .. import config
from ..normalization import coerce_number, normalize_code
from ..strategies.types import storage_strategy_name

LOCAL_WEIGHT = 0.75
DEEPSEEK_WEIGHT = 0.25
_HARD_RISK_TERMS = ("减持", "解禁", "监管", "诉讼", "退市", "财务恶化", "重大违法")
_EXECUTION_STATE_FIELDS = (
    "execution_allowed",
    "non_executable_reason",
    "tier",
    "tier_label",
    "trade_action",
)


def production_enabled() -> bool:
    return bool(getattr(config, "DEEPSEEK_PRODUCTION_SCORING_ENABLED", True)) and not bool(
        getattr(config, "DEEPSEEK_SHADOW_ONLY", False)
    )


def deepseek_decision(feature: dict[str, object], strategy_name: str) -> dict[str, object]:
    strategy = storage_strategy_name(strategy_name)
    feature = feature if isinstance(feature, dict) else {}
    abstain = bool(feature.get("abstain"))
    covered = _valid_production_feature(feature) and not abstain
    score = _clamp(feature.get("deepseek_score"), 0.0, 100.0) if covered else None
    penalty = _clamp(feature.get("risk_penalty"), 0.0, 30.0) if covered else 0.0
    strategy_fit = bool(feature.get("strategy_fit")) if covered else False
    horizon_fit = bool(feature.get("horizon_fit", feature.get("horizon_match"))) if covered else False
    risk_flags = _string_list(feature.get("risk_flags"))
    raw_risk_section = feature.get("risk_assessment")
    risk_section: dict[str, object] = raw_risk_section if isinstance(raw_risk_section, dict) else {}
    verified_strong_risk = str(risk_section.get("risk_level") or "") == "high" and any(
        term in " ".join(risk_flags + _string_list(risk_section.get("flags"))) for term in _HARD_RISK_TERMS
    )
    veto = bool(feature.get("veto") or verified_strong_risk) if covered else False
    selected = covered and strategy_fit and horizon_fit and not veto and coerce_number(score) >= 60.0
    return {
        "strategy": strategy,
        "covered": covered,
        "abstain": abstain,
        "deepseek_score": score,
        "risk_penalty": penalty,
        "strategy_fit": strategy_fit,
        "horizon_fit": horizon_fit,
        "veto": veto,
        "selected": selected,
        "reason": str(feature.get("reason") or "")[:240],
        "risk_flags": risk_flags,
    }


def merge_row_score(row: dict[str, object], strategy_name: str) -> dict[str, object]:
    result = dict(row or {})
    _restore_execution_state(result)
    local_score = _clamp(result.get("local_score", result.get("score")), 0.0, 100.0)
    local_ranking_source = str(result.get("local_ranking_source") or result.get("ranking_source") or "local_only")
    raw_feature = result.get("deepseek_features")
    feature: dict[str, object] = raw_feature if isinstance(raw_feature, dict) else {}
    decision = deepseek_decision(feature, strategy_name)
    production_applied = production_enabled() and decision["covered"]
    effective_selected = production_applied and bool(decision["selected"])
    effective_veto = production_applied and bool(decision["veto"])
    effective_period_mismatch = production_applied and (
        not bool(decision["strategy_fit"]) or not bool(decision["horizon_fit"])
    )
    local_risk_penalty = _clamp(
        result.get("local_risk_penalty", result.get("risk_penalty")),
        0.0,
        30.0,
    )
    combined_risk_penalty = (
        min(30.0, local_risk_penalty + coerce_number(decision["risk_penalty"]))
        if production_applied
        else local_risk_penalty
    )
    if production_applied:
        final_score = local_score * LOCAL_WEIGHT + coerce_number(decision["deepseek_score"]) * DEEPSEEK_WEIGHT
        final_score -= combined_risk_penalty
    else:
        final_score = local_score
    final_score = round(max(0.0, min(100.0, final_score)), 2)

    result.update(
        local_score=round(local_score, 2),
        local_risk_penalty=round(local_risk_penalty, 2),
        deepseek_score=decision["deepseek_score"],
        deepseek_risk_penalty=round(coerce_number(decision["risk_penalty"]), 2),
        risk_penalty=round(combined_risk_penalty, 2),
        final_score=final_score,
        score=final_score,
        deepseek_selected=effective_selected,
        deepseek_veto=effective_veto,
        deepseek_strategy_fit=bool(decision["strategy_fit"]),
        deepseek_horizon_fit=bool(decision["horizon_fit"]),
        deepseek_period_mismatch=effective_period_mismatch,
        deepseek_reason=decision["reason"],
        deepseek_risk_flags=decision["risk_flags"],
        deepseek_production_applied=production_applied,
        local_ranking_source=local_ranking_source,
        ranking_source="local_75_deepseek_25" if production_applied else local_ranking_source,
    )
    result.setdefault("deepseek_feature_status", "local_only")
    section_defaults = {
        "value_quality": {"assessment": "unknown", "confidence": 0.0, "flags": []},
        "financial_health": {
            "profit_trend": "unknown",
            "cashflow_trend": "unknown",
            "confidence": 0.0,
            "flags": [],
        },
        "market_flow": {
            "flow_health": "unknown",
            "price_flow_divergence": False,
            "confidence": 0.0,
            "flags": [],
        },
        "industry_policy": {
            "industry_outlook": "unknown",
            "policy_relevance": "unknown",
            "confidence": 0.0,
            "flags": [],
        },
        "risk_assessment": {"risk_level": "unknown", "confidence": 0.0, "flags": []},
    }
    neutral_features = {
        "abstain": True,
        "reason": "No valid precomputed DeepSeek review.",
        **{section: dict(default) for section, default in section_defaults.items()},
    }
    result.setdefault("deepseek_features", neutral_features)
    for section, default in section_defaults.items():
        raw_section = feature.get(section)
        result[f"deepseek_{section}"] = dict(raw_section) if isinstance(raw_section, dict) else dict(default)
    return result


def merge_and_rank_rows(
    rows: Iterable[dict[str, object]],
    strategy_name: str,
    *,
    now: datetime | None = None,
) -> list[dict[str, object]]:
    merged = [merge_row_score(row, strategy_name) for row in rows or [] if isinstance(row, dict)]
    merged.sort(
        key=lambda row: (
            bool(row.get("deepseek_veto") or row.get("deepseek_period_mismatch")),
            -coerce_number(row.get("final_score")),
            -coerce_number(row.get("local_score")),
            str(row.get("code") or ""),
        )
    )
    for rank, row in enumerate(merged, start=1):
        row["selection_rank"] = rank
        row["rank"] = rank
        row["display_rank"] = rank
        _apply_execution_safety(row, storage_strategy_name(strategy_name), now or datetime.now())
    return merged


def attach_and_merge_rows(
    rows: Iterable[dict[str, object]],
    strategy_name: str,
    validation_store: object,
    *,
    signal_time: str = "",
    cutoff_at: str = "",
    attach_features: Callable[..., list[dict[str, object]]] | None = None,
) -> list[dict[str, object]]:
    if attach_features is None:
        from .runtime_features import attach_persisted_deepseek_features

        attach_features = attach_persisted_deepseek_features
    source_rows = [dict(row) for row in rows or [] if isinstance(row, dict)]
    review_limit = max(80, min(120, int(getattr(config, "DEEPSEEK_CANDIDATE_POOL_LIMIT", 120))))
    review_pool = _research_candidate_pool(source_rows, review_limit)
    attached = attach_features(
        review_pool,
        strategy_name,
        validation_store,
        signal_time=signal_time,
        cutoff_at=cutoff_at,
    )
    attached_by_code = {normalize_code(row.get("code")): row for row in attached}
    materialized = []
    for row in source_rows:
        code = normalize_code(row.get("code"))
        item = dict(attached_by_code.get(code, row))
        if code not in attached_by_code:
            item.setdefault("deepseek_feature_status", "local_only")
        materialized.append(item)
    timestamp = _parse_datetime(signal_time) or datetime.now()
    return merge_and_rank_rows(materialized, strategy_name, now=timestamp)


def select_top_rows(
    rows: Iterable[dict[str, object]],
    strategy_name: str,
    limit: int,
    *,
    include_veto_observations: bool = True,
) -> list[dict[str, object]]:
    ranked = merge_and_rank_rows(rows, strategy_name)
    executable = [row for row in ranked if not row.get("deepseek_veto")]
    selected = executable[: max(0, int(limit or 0))]
    if include_veto_observations and len(selected) < max(0, int(limit or 0)):
        selected.extend(row for row in ranked if row.get("deepseek_veto"))
        selected = selected[: max(0, int(limit or 0))]
    for rank, row in enumerate(selected, start=1):
        row["rank"] = rank
        row["display_rank"] = rank
    return selected


def today_phase(timestamp: datetime | None = None) -> dict[str, object]:
    now = timestamp or datetime.now()
    current = now.time().replace(second=0, microsecond=0)
    if time(9, 30) <= current < time(9, 36):
        return {
            "code": "open_observe",
            "label": "开盘观察期",
            "execution_allowed": False,
            "minimum_final_score": None,
        }
    if time(9, 36) <= current < time(10, 30):
        return {
            "code": "main_execution",
            "label": "主执行窗口",
            "execution_allowed": True,
            "minimum_final_score": coerce_number(getattr(config, "TODAY_RECOMMENDATION_MIN_SCORE", 60.0)),
        }
    if time(10, 30) <= current <= time(11, 20):
        return {
            "code": "late_execution",
            "label": "降级执行窗口",
            "execution_allowed": True,
            "minimum_final_score": coerce_number(getattr(config, "TODAY_LATE_MIN_FINAL_SCORE", 68.0)),
        }
    if time(13, 0) <= current <= time(14, 0):
        return {
            "code": "afternoon_observe",
            "label": "午后观察",
            "execution_allowed": False,
            "minimum_final_score": None,
        }
    return {
        "code": "observe_only",
        "label": "仅观察",
        "execution_allowed": False,
        "minimum_final_score": None,
    }


def deepseek_meta_for_rows(
    rows: Iterable[dict[str, object]],
    strategy_name: str,
) -> dict[str, object]:
    materialized = [row for row in rows or [] if isinstance(row, dict)]
    reviewed = sum(
        1
        for row in materialized
        if str(row.get("deepseek_feature_status") or "") in {"precomputed", "abstain", "cache_hit"}
    )
    applied = sum(1 for row in materialized if row.get("deepseek_production_applied"))
    abstain = sum(1 for row in materialized if str(row.get("deepseek_feature_status") or "") == "abstain")
    requested = len(materialized)
    return {
        "enabled": bool(getattr(config, "ENABLE_DEEPSEEK_FEATURES", True)),
        "production_applied": bool(production_enabled() and applied),
        "weight": DEEPSEEK_WEIGHT,
        "strategy": storage_strategy_name(strategy_name),
        "status": "precomputed" if applied else "abstain" if abstain == requested and requested else "local_only",
        "requested": requested,
        "reviewed": reviewed,
        "coverage_pct": round(reviewed * 100.0 / max(1, requested), 2),
        "abstain_count": abstain,
    }


def ranking_groups(rows: Iterable[dict[str, object]], top_k: int = 5) -> dict[str, list[str]]:
    materialized = [row for row in rows or [] if isinstance(row, dict)]

    def codes(field: str, *, reject_veto: bool = False) -> list[str]:
        candidates = [row for row in materialized if not reject_veto or not row.get("deepseek_veto")]
        if field == "deepseek_score":
            candidates = [row for row in candidates if row.get("deepseek_score") is not None]
        candidates.sort(key=lambda row: -coerce_number(row.get(field)))
        return [str(row.get("code") or "") for row in candidates[: max(0, int(top_k or 0))] if row.get("code")]

    return {
        "local_top_codes": codes("local_score"),
        "deepseek_top_codes": codes("deepseek_score", reject_veto=True),
        "final_top_codes": codes("final_score", reject_veto=True),
    }


def _apply_execution_safety(row: dict[str, object], strategy: str, now: datetime) -> None:
    reason = ""
    if row.get("eligible") is False or row.get("local_hard_filter_passed") is False:
        reason = "本地硬过滤未通过，仅保留观察"
    elif row.get("deepseek_veto"):
        reason = "DeepSeek强风险veto，仅保留观察"
    elif row.get("deepseek_period_mismatch"):
        reason = "DeepSeek判断与当前策略周期不匹配，仅保留观察"
    elif strategy == "today_term":
        phase = today_phase(now)
        row["today_phase"] = phase["code"]
        row["today_phase_label"] = phase["label"]
        minimum = phase.get("minimum_final_score")
        if not phase["execution_allowed"]:
            reason = f"{phase['label']}不产生可执行推荐"
        elif minimum is not None and coerce_number(row.get("final_score")) < coerce_number(minimum):
            reason = f"{phase['label']}综合分未达到{coerce_number(minimum):.0f}分"
        elif (
            phase["code"] == "late_execution"
            and _is_high_risk_candidate(row)
            and str(row.get("deepseek_feature_status") or "")
            not in {
                "precomputed",
                "cache_hit",
            }
        ):
            reason = "降级执行窗口高风险候选未经DeepSeek覆盖，仅观察"
    if not reason:
        row.pop("_deepseek_execution_state", None)
        return
    state = row.get("_deepseek_execution_state")
    if not isinstance(state, dict):
        state = {"original": _execution_snapshot(row)}
        row["_deepseek_execution_state"] = state
    row["execution_allowed"] = False
    row["non_executable_reason"] = reason
    row["tier"] = "backup_pool"
    row["tier_label"] = "风险观察"
    row["trade_action"] = {
        "action": "watch_only",
        "label": "只观察",
        "position_size": 0.0,
        "reason": reason,
    }
    state["applied"] = _execution_snapshot(row)


def _execution_snapshot(row: dict[str, object]) -> dict[str, object]:
    return {key: row[key] for key in _EXECUTION_STATE_FIELDS if key in row}


def _restore_execution_state(row: dict[str, object]) -> None:
    state = row.get("_deepseek_execution_state")
    if not isinstance(state, dict):
        return
    applied = state.get("applied")
    if not isinstance(applied, dict):
        return
    current = _execution_snapshot(row)
    if current != applied:
        # A local validation gate changed the row after the previous merge;
        # preserve that newer state as the next restoration baseline.
        state["original"] = current
        state.pop("applied", None)
        return
    original = state.get("original") if isinstance(state.get("original"), dict) else {}
    for key in _EXECUTION_STATE_FIELDS:
        row.pop(key, None)
    row.update(original)
    state.pop("applied", None)


def _parse_datetime(value: object) -> datetime | None:
    text = str(value or "").strip().replace(" ", "T", 1)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _is_high_risk_candidate(row: dict[str, object]) -> bool:
    raw_sell_risk = row.get("sell_risk")
    sell_risk: dict[str, object] = raw_sell_risk if isinstance(raw_sell_risk, dict) else {}
    risk_score = coerce_number(sell_risk.get("score") if sell_risk else row.get("avg_risk"))
    return bool(
        risk_score >= 60.0
        or coerce_number(row.get("local_risk_penalty", row.get("risk_penalty"))) >= 8.0
        or row.get("announcement_flags")
        or row.get("event_risk_flags")
        or row.get("risk_words")
    )


def _research_candidate_pool(rows: list[dict[str, object]], limit: int) -> list[dict[str, object]]:
    if len(rows) <= limit:
        return list(rows)
    ordered = sorted(rows, key=lambda row: -coerce_number(row.get("score")))
    result: list[dict[str, object]] = []
    seen = set()

    def add(candidates: Iterable[dict[str, object]], allocation: int) -> None:
        added = 0
        for row in candidates:
            code = normalize_code(row.get("code"))
            if not code or code in seen:
                continue
            seen.add(code)
            result.append(row)
            added += 1
            if added >= allocation or len(result) >= limit:
                return

    add(ordered, max(1, limit // 2))
    risk_rows = [
        row
        for row in ordered
        if row.get("announcement_flags")
        or row.get("event_risk_flags")
        or row.get("risk_words")
        or row.get("sell_risk")
        or coerce_number(row.get("risk_penalty")) > 0
    ]
    add(risk_rows, max(1, limit // 5))
    evidence_rows = [
        row
        for row in ordered
        if row.get("recent_news")
        or row.get("announcement_time")
        or row.get("policy_support_score")
        or row.get("fundamental_value_score")
    ]
    add(evidence_rows, max(1, limit // 5))
    add(ordered, limit - len(result))
    return result[:limit]


def _clamp(value: object, low: float, high: float) -> float:
    return max(low, min(high, float(coerce_number(value, low))))


def _valid_production_feature(feature: dict[str, object]) -> bool:
    if not feature or feature.get("valid") is False or not isinstance(feature.get("abstain"), bool):
        return False
    if any(not isinstance(feature.get(key), bool) for key in ("strategy_fit", "horizon_fit", "veto")):
        return False
    return all(
        isinstance(feature.get(key), (int, float)) and not isinstance(feature.get(key), bool)
        for key in ("deepseek_score", "risk_penalty")
    )


def _string_list(value: object) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item) for item in value if str(item).strip()]


__all__ = [
    "DEEPSEEK_WEIGHT",
    "LOCAL_WEIGHT",
    "attach_and_merge_rows",
    "deepseek_decision",
    "deepseek_meta_for_rows",
    "merge_and_rank_rows",
    "merge_row_score",
    "production_enabled",
    "ranking_groups",
    "select_top_rows",
    "today_phase",
]
