"""Deterministic recommendation policies shared by delivery paths."""

from __future__ import annotations

from typing import Dict, List, Tuple


def _code(row: Dict[str, object]) -> str:
    return str(row.get("code") or "").strip()


def apply_today_next_day_gate(
    recommendations_by_horizon: Dict[str, List[Dict[str, object]]],
) -> Tuple[Dict[str, List[Dict[str, object]]], Dict[str, object]]:
    """Backward-compatible no-op.

    Historical logic removed the hard down-grade gate between today/short and next-day
    strategies; keep return shape stable for existing callers.
    """
    recommendations = dict(recommendations_by_horizon or {})
    return recommendations, {
        "enabled": False,
        "rule": "disabled",
        "reason": "短期策略不再与明日策略强绑定门控",
    }
