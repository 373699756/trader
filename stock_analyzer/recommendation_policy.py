"""Deterministic recommendation policies shared by delivery paths."""

from __future__ import annotations

from typing import Dict, List, Tuple


_NEXT_DAY_KEY_MARKERS = ("tomorrow", "next_day", "nextday")


def _set_reason(row: Dict[str, object], reason: str) -> None:
    if not reason:
        return
    reasons = row.get("reasons")
    if not isinstance(reasons, list):
        reasons = []
    reason_text = str(reason).strip()
    if reason_text and reason_text not in reasons:
        reasons.append(reason_text)
    row["reasons"] = reasons[:8]


def _ensure_dict(row: Dict[str, object], key: str) -> Dict[str, object]:
    value = row.get(key)
    if isinstance(value, dict):
        return value
    return {}


def _mark_short_term_unconfirmed(row: Dict[str, object]) -> None:
    row["tier"] = "backup_pool"
    row["tier_label"] = "盘中观察"
    row["execution_allowed"] = False
    row["today_next_day_gate_status"] = "unconfirmed"
    row["execution_block_reason"] = "未与明日优先策略重合，转为观察"
    action = _ensure_dict(row, "trade_action")
    action["action"] = "watch_only"
    action["label"] = "只观察"
    action["position_size"] = 0.0
    action["reason"] = "未与明日优先策略重合，转为观察"
    row["trade_action"] = action
    row["recommendation_class"] = "today_continuation_backup_watch"
    row["recommendation_class_label"] = "今日延续备选观察"
    row["prediction_type"] = "rank_score"
    row["score_note"] = "今日延续重点观察未与明日优先重合，降级为备选观察。"
    _set_reason(row, "未与明日优先策略重合，转为观察")
    row["execution_status_updated_at"] = row.get("execution_status_updated_at") or ""


def apply_today_next_day_gate(
    recommendations_by_horizon: Dict[str, List[Dict[str, object]]],
) -> Tuple[Dict[str, List[Dict[str, object]]], Dict[str, object]]:
    """Require every today pick to also appear in the next-day strategy."""
    recommendations = dict(recommendations_by_horizon or {})
    tomorrow_key = next(
        (
            key
            for key in recommendations
            if key != "short_term"
            and any(marker in str(key).lower() for marker in _NEXT_DAY_KEY_MARKERS)
        ),
        None,
    )
    tomorrow_codes = set()
    if tomorrow_key is not None:
        tomorrow_codes = {
            _code(row)
            for row in recommendations.get(tomorrow_key, [])
            if isinstance(row, dict) and _code(row)
        }
    short_rows = []
    for row in recommendations.get("short_term", []):
        if not isinstance(row, dict):
            continue
        short_rows.append(row)
        if _code(row) in tomorrow_codes:
            row["today_next_day_gate_status"] = "confirmed"
            row["execution_status_updated_at"] = row.get("execution_status_updated_at") or ""
            continue
        _mark_short_term_unconfirmed(row)
    recommendations["short_term"] = short_rows
    confirmed_count = sum(1 for row in short_rows if row.get("today_next_day_gate_status") == "confirmed")
    return recommendations, {
        "enabled": True,
        "confirmation_strategy": str(tomorrow_key or "unavailable"),
        "confirmed_code_count": confirmed_count,
        "short_term_candidate_count": len(short_rows),
        "tomorrow_candidate_count": len(tomorrow_codes),
        "executability_demotion_count": len(short_rows) - confirmed_count,
        "rule": "today_continuation_and_next_day_continuation",
    }


def _code(row: Dict[str, object]) -> str:
    return str(row.get("code") or "").strip()
