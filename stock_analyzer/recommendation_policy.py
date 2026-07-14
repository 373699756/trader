"""Deterministic recommendation policies shared by delivery paths."""

from __future__ import annotations

from typing import Dict, List, Tuple


_NEXT_DAY_KEY_MARKERS = ("tomorrow", "next_day", "nextday")


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
    recommendations["short_term"] = [
        row
        for row in recommendations.get("short_term", [])
        if isinstance(row, dict) and _code(row) in tomorrow_codes
    ]
    return recommendations, {
        "enabled": True,
        "confirmation_strategy": str(tomorrow_key or "unavailable"),
        "confirmed_code_count": len(tomorrow_codes),
        "rule": "today_continuation_and_next_day_continuation",
    }


def _code(row: Dict[str, object]) -> str:
    return str(row.get("code") or "").strip()

