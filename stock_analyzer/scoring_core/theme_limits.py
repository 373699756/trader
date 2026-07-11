from __future__ import annotations

from typing import Dict, List, Tuple

import pandas as pd

from . import base as _base


__all__ = [
    "CHOKEPOINT_CHAIN",
    "CHOKEPOINT_INDUSTRY_LEADERS",
    "CHOKEPOINT_KEYWORDS",
    "SERENITY_REFERENCES",
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


def __getattr__(name: str):
    if name in __all__:
        return getattr(_base, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _tomorrow_theme_key(row: Dict[str, object]) -> str:
    return _base._tomorrow_theme_key(row)


def limit_theme_concentration(
    rows: List[Dict[str, object]],
    limit: int,
    cap: int = None,
) -> Tuple[List[Dict[str, object]], int]:
    return _base.limit_theme_concentration(rows, limit, cap=cap)


def _infer_theme_from_row(row: Dict[str, object]) -> str:
    return _base._infer_theme_from_row(row)


def _theme_round_robin(
    rows: List[Dict[str, object]],
    limit: int,
    cap: int,
) -> Tuple[List[Dict[str, object]], int]:
    return _base._theme_round_robin(rows, limit, cap)


def _theme_count_allowed(counts: Dict[str, int], theme_key: str, cap) -> bool:
    return _base._theme_count_allowed(counts, theme_key, cap)


def _tomorrow_display_theme_allowed(rows: List[Dict[str, object]], row: Dict[str, object]) -> bool:
    return _base._tomorrow_display_theme_allowed(rows, row)


def _limit_tomorrow_display_concentration(
    rows: List[Dict[str, object]],
    limit: int,
) -> List[Dict[str, object]]:
    return _base._limit_tomorrow_display_concentration(rows, limit)


def _tomorrow_theme_distribution(rows: List[Dict[str, object]]) -> Dict[str, int]:
    return _base._tomorrow_theme_distribution(rows)
