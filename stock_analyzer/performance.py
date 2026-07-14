from __future__ import annotations

import json
from typing import Dict, Iterable, List, Optional, Tuple

import pandas as pd


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def validation_metrics_cache_key(strategy_name: str, baseline_id: str, days: int) -> Tuple[str, str, int]:
    return (
        str(strategy_name or "").strip(),
        str(baseline_id or "").strip(),
        _safe_int(days),
    )


def records_from_columns(
    df: pd.DataFrame,
    columns: Iterable[str],
    *,
    limit: Optional[int] = None,
    sort_by: str = "",
    ascending: bool = True,
) -> List[Dict[str, object]]:
    if df is None or df.empty:
        return []
    selected_columns = [column for column in columns if column in df.columns]
    if not selected_columns:
        return []

    frame = df
    row_limit = None if limit is None else max(0, _safe_int(limit))
    if sort_by and sort_by in df.columns:
        if row_limit is not None:
            try:
                frame = df.nsmallest(row_limit, sort_by) if ascending else df.nlargest(row_limit, sort_by)
                row_limit = None
            except (TypeError, ValueError):
                frame = df.sort_values(sort_by, ascending=ascending, kind="mergesort")
        else:
            frame = df.sort_values(sort_by, ascending=ascending, kind="mergesort")
    if row_limit is not None:
        frame = frame.head(row_limit)
    return frame.loc[:, selected_columns].to_dict("records")


def json_loads_cached(raw: object, cache: Optional[Dict[str, object]] = None, default=None):
    if isinstance(raw, str):
        text = raw or "{}"
    elif raw is None:
        text = "{}"
    else:
        return raw
    if cache is not None and text in cache:
        return cache[text]
    try:
        value = json.loads(text)
    except Exception:
        value = {} if default is None else default
    if cache is not None:
        cache[text] = value
    return value



