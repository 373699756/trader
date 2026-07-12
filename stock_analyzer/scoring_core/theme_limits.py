from __future__ import annotations

from typing import Dict, List, Tuple

import pandas as pd

from .. import config
from ..normalization import coerce_number
from .theme_constants import CHOKEPOINT_CHAIN, TECH_THEMES


__all__ = [
    "CHOKEPOINT_CHAIN",
    "TECH_THEMES",
    "limit_theme_concentration",
    "_infer_theme_from_row",
    "_limit_tomorrow_display_concentration",
    "_theme_count_allowed",
    "_theme_round_robin",
    "_tomorrow_display_theme_allowed",
    "_tomorrow_theme_distribution",
    "_tomorrow_theme_key",
]


def _tomorrow_theme_key(row: Dict[str, object]) -> str:
    theme = str(row.get("theme") or "").strip()
    if theme:
        return theme
    industry = str(row.get("industry") or "").strip()
    if industry:
        return industry
    inferred = _infer_theme_from_row(row)
    if inferred:
        return inferred
    code = str(row.get("code") or "").strip()
    return "未分类:{}".format(code or "unknown")


def limit_theme_concentration(
    rows: List[Dict[str, object]],
    limit: int,
    cap: int = None,
) -> Tuple[List[Dict[str, object]], int]:
    display_limit = max(0, int(limit or 0))
    theme_cap = int(coerce_number(cap, getattr(config, "RECOMMENDATION_MAX_DISPLAY_PER_THEME", 3)))
    return _theme_round_robin(rows, display_limit, theme_cap)


def _infer_theme_from_row(row: Dict[str, object]) -> str:
    haystack = "{} {}".format(row.get("name", ""), row.get("industry", "")).upper()
    if not haystack.strip():
        return ""
    for segment in CHOKEPOINT_CHAIN:
        if any(str(keyword).upper() in haystack for keyword in segment.get("keywords", ())):
            return str(segment.get("segment") or "").strip()
    for theme, keywords in TECH_THEMES.items():
        if any(str(keyword).upper() in haystack for keyword in keywords):
            return theme
    return ""


def _theme_round_robin(
    rows: List[Dict[str, object]],
    limit: int,
    cap: int,
) -> Tuple[List[Dict[str, object]], int]:
    display_limit = max(0, int(limit or 0))
    if display_limit <= 0:
        return [], len(rows or [])
    theme_cap = int(coerce_number(cap, 0))
    if theme_cap <= 0:
        return list(rows or [])[:display_limit], max(0, len(rows or []) - display_limit)
    groups: Dict[str, List[Dict[str, object]]] = {}
    theme_order: List[str] = []
    for row in rows or []:
        key = _tomorrow_theme_key(row)
        if key not in groups:
            groups[key] = []
            theme_order.append(key)
        groups[key].append(row)
    selected: List[Dict[str, object]] = []
    round_index = 0
    while len(selected) < display_limit:
        added = False
        for key in theme_order:
            group = groups.get(key) or []
            if round_index >= min(len(group), theme_cap):
                continue
            selected.append(group[round_index])
            added = True
            if len(selected) >= display_limit:
                break
        if not added:
            break
        round_index += 1
    limited_by_theme = sum(max(0, len(group) - theme_cap) for group in groups.values())
    limited_by_limit = max(0, sum(min(len(group), theme_cap) for group in groups.values()) - len(selected))
    return selected, limited_by_theme + limited_by_limit


def _theme_count_allowed(counts: Dict[str, int], theme_key: str, cap) -> bool:
    limit = int(coerce_number(cap, 0))
    if limit <= 0:
        return True
    return counts.get(theme_key, 0) < limit


def _tomorrow_display_theme_allowed(rows: List[Dict[str, object]], row: Dict[str, object]) -> bool:
    limit = int(coerce_number(getattr(config, "TOMORROW_MAX_DISPLAY_PER_THEME", 5), 5))
    if limit <= 0:
        return True
    key = _tomorrow_theme_key(row)
    return sum(1 for item in rows if _tomorrow_theme_key(item) == key) < limit


def _append_unique_reason(row: Dict[str, object], reason: str) -> None:
    text = str(reason or "").strip()
    if not text:
        return
    reasons = list(row.get("reasons") or [])
    if text not in reasons:
        reasons.append(text)
    row["reasons"] = reasons[:8]


def _limit_tomorrow_display_concentration(
    rows: List[Dict[str, object]],
    limit: int,
) -> List[Dict[str, object]]:
    theme_cap = int(coerce_number(getattr(config, "TOMORROW_MAX_DISPLAY_PER_THEME", 5), 5))
    selected, _ = _theme_round_robin(rows, limit, theme_cap)
    selected_ids = {id(row) for row in selected}
    for row in rows:
        if id(row) not in selected_ids:
            _append_unique_reason(row, "行业/主题分散展示未入选")
    return selected[:limit]


def _tomorrow_theme_distribution(rows: List[Dict[str, object]]) -> Dict[str, int]:
    distribution: Dict[str, int] = {}
    for row in rows:
        key = _tomorrow_theme_key(row)
        distribution[key] = distribution.get(key, 0) + 1
    return dict(sorted(distribution.items(), key=lambda item: (-item[1], item[0]))[:8])
