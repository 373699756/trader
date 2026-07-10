import json
import os
import time
from datetime import datetime
from typing import Dict, Iterable

import pandas as pd

from . import config
from .normalization import coerce_number, finite_series, normalize_code, percentile_score
from .runtime_json import atomic_write_json


FUNDAMENTAL_COLUMNS = (
    "roe",
    "gross_margin",
    "debt_ratio",
    "pe_dynamic",
    "pb",
    "earnings_surprise",
    "rating_revision",
)


def load_fundamentals(provider=None, codes: Iterable[str] = None, force: bool = False) -> Dict[str, object]:
    if not getattr(config, "ENABLE_FUNDAMENTALS", False):
        return {"enabled": False, "status": "disabled", "items": {}, "generated_at": ""}
    code_list = [normalize_code(code) for code in (codes or []) if normalize_code(code)]
    cached = _load_cache()
    cached_items = cached.get("items") if isinstance(cached, dict) else {}
    cache_covers_request = not code_list or all(code in (cached_items or {}) for code in code_list)
    if cached and not force and cache_covers_request:
        return {**cached, "enabled": True, "status": cached.get("status", "cached")}
    if provider is None or not hasattr(provider, "get_fundamental_factors"):
        return {"enabled": True, "status": "no_provider", "items": {}, "generated_at": ""}
    if not code_list:
        return {"enabled": True, "status": "no_codes", "items": {}, "generated_at": ""}
    try:
        raw_items = provider.get_fundamental_factors(codes=code_list)
        items = _normalize_fundamental_items(raw_items)
        payload = {
            "enabled": True,
            "status": "ok" if items else "empty",
            "items": items,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        }
        _save_cache(payload)
        return payload
    except Exception as exc:
        return {"enabled": True, "status": "error", "error": str(exc), "items": {}, "generated_at": ""}


def attach_fundamental_factors(df: pd.DataFrame, fundamentals: Dict[str, Dict[str, object]] = None) -> pd.DataFrame:
    if df is None or df.empty or not getattr(config, "ENABLE_FUNDAMENTALS", False):
        return df
    result = df.copy()
    fundamentals = (fundamentals or {}).get("items", fundamentals or {}) if isinstance(fundamentals, dict) else {}
    if "code" in result.columns and fundamentals:
        for column in FUNDAMENTAL_COLUMNS:
            if column not in result.columns:
                result[column] = result["code"].map(lambda code: fundamentals.get(normalize_code(code), {}).get(column, 0.0))
    for column in FUNDAMENTAL_COLUMNS:
        if column not in result.columns:
            result[column] = 0.0
        result[column] = result[column].map(coerce_number)

    roe_values = _nonzero_values(result, "roe")
    gross_values = _nonzero_values(result, "gross_margin")
    debt_values = _nonzero_values(result, "debt_ratio")
    pe_values = [value for value in finite_series(result, "pe_dynamic").tolist() if value > 0]
    pb_values = [value for value in finite_series(result, "pb").tolist() if value > 0]
    surprise_values = _nonzero_values(result, "earnings_surprise")
    revision_values = _nonzero_values(result, "rating_revision")
    degraded = not any((roe_values, gross_values, debt_values, pe_values, pb_values, surprise_values, revision_values))

    quality_scores = []
    value_scores = []
    surprise_scores = []
    revision_scores = []
    for _, row in result.iterrows():
        quality = _neutral_if_missing(row.get("roe"), roe_values) * 0.45
        quality += _neutral_if_missing(row.get("gross_margin"), gross_values) * 0.35
        quality += _neutral_if_missing(row.get("debt_ratio"), debt_values, higher_is_better=False) * 0.20
        pe = coerce_number(row.get("pe_dynamic"))
        pb = coerce_number(row.get("pb"))
        value = (
            percentile_score(pe, pe_values, higher_is_better=False) * 0.55
            + percentile_score(pb, pb_values, higher_is_better=False) * 0.45
            if pe > 0 and pb > 0
            else 50.0
        )
        quality_scores.append(round(quality, 2))
        value_scores.append(round(value, 2))
        surprise_scores.append(round(_neutral_if_missing(row.get("earnings_surprise"), surprise_values), 2))
        revision_scores.append(round(_neutral_if_missing(row.get("rating_revision"), revision_values), 2))
    result["fundamental_quality_score"] = quality_scores
    result["fundamental_value_score"] = value_scores
    result["earnings_surprise_score"] = surprise_scores
    result["rating_revision_score"] = revision_scores
    result["fundamental_status"] = "degraded" if degraded else "enabled"
    result["fundamental_degraded"] = bool(degraded)
    return result


def _nonzero_values(df: pd.DataFrame, column: str) -> list:
    return [value for value in finite_series(df, column).tolist() if abs(coerce_number(value)) > 1e-12]


def _neutral_if_missing(value: float, values: list, higher_is_better: bool = True) -> float:
    numeric = coerce_number(value)
    if not values or abs(numeric) <= 1e-12:
        return 50.0
    return percentile_score(numeric, values, higher_is_better=higher_is_better)


def _normalize_fundamental_items(raw_items) -> Dict[str, Dict[str, object]]:
    if not raw_items:
        return {}
    if isinstance(raw_items, dict):
        iterable = raw_items.items()
        rows = []
        for code, item in iterable:
            row = dict(item or {})
            row["code"] = code
            rows.append(row)
    else:
        rows = list(raw_items or [])
    items: Dict[str, Dict[str, object]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = normalize_code(row.get("code") or row.get("ts_code") or row.get("股票代码") or row.get("证券代码"))
        if not code:
            continue
        items[code] = {column: coerce_number(row.get(column)) for column in FUNDAMENTAL_COLUMNS}
    return items


def _load_cache() -> Dict[str, object]:
    path = getattr(config, "FUNDAMENTAL_CACHE_PATH", ".runtime/fundamentals.json")
    try:
        if not os.path.exists(path):
            return {}
        max_age = int(getattr(config, "FUNDAMENTAL_CACHE_HOURS", 24)) * 3600
        if time.time() - os.path.getmtime(path) > max_age:
            return {}
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _save_cache(payload: Dict[str, object]) -> None:
    path = getattr(config, "FUNDAMENTAL_CACHE_PATH", ".runtime/fundamentals.json")
    try:
        atomic_write_json(path, payload, ensure_ascii=False, indent=2)
    except Exception:
        return
