from __future__ import annotations

from typing import Dict, Iterable, List

import pandas as pd

from . import config
from .normalization import coerce_number, normalize_code, rename_known_columns


class CandidateBenchmarkCalculator:
    """Build same-cycle market, industry and style peer benchmarks."""

    def __init__(self, provider, candidates: Iterable[Dict[str, object]]) -> None:
        self.provider = provider
        self.candidates = [
            row
            for row in candidates or []
            if row.get("eligible") and row.get("point_in_time_valid", True) and normalize_code(row.get("code"))
        ]
        self._history: Dict[str, pd.DataFrame] = {}
        self._cycle_returns: Dict[tuple, Dict[str, float]] = {}

    def calculate(self, signal, outcome: Dict[str, object], primary_return_field: str) -> Dict[str, object]:
        entry_date = _date_key(outcome.get("next_trade_date"))
        exit_date = entry_date
        if primary_return_field in {"exit_return", "signal_exit_return"}:
            exit_date = _date_key(outcome.get("exit_date")) or entry_date
        period = {"entry_date": entry_date, "exit_date": exit_date, "entry": "open", "exit": "close"}
        if not entry_date or not exit_date or not self.candidates:
            return _unknown_benchmarks(period, "candidate_pool_unavailable")

        cycle_returns = self._returns_for_cycle(entry_date, exit_date)
        industry = str(_mapping_get(signal, "theme", "") or "")
        raw = _raw_signal(signal)
        industry = str(raw.get("industry") or raw.get("theme") or industry)
        style = str(raw.get("style_bucket") or "")
        if not style:
            style = _candidate_value(self.candidates, normalize_code(_mapping_get(signal, "code", "")), "style_bucket")
        if not industry:
            industry = _candidate_value(self.candidates, normalize_code(_mapping_get(signal, "code", "")), "industry")

        return {
            "period": period,
            "market": self._group_summary(cycle_returns, self.candidates, lambda _row: True, "eligible_pool_equal_weight"),
            "industry": self._group_summary(
                cycle_returns,
                self.candidates,
                lambda row: bool(industry) and str(row.get("industry") or "") == industry,
                "industry_peer_equal_weight",
                group=industry,
            ),
            "style": self._group_summary(
                cycle_returns,
                self.candidates,
                lambda row: bool(style) and str(row.get("style_bucket") or "") == style,
                "style_peer_equal_weight",
                group=style,
            ),
        }

    def _returns_for_cycle(self, entry_date: str, exit_date: str) -> Dict[str, float]:
        key = (entry_date, exit_date)
        if key in self._cycle_returns:
            return self._cycle_returns[key]
        limit = max(1, int(getattr(config, "VALIDATION_BENCHMARK_MAX_PEERS", 120)))
        rows = sorted(self.candidates, key=lambda row: normalize_code(row.get("code")))[:limit]
        result: Dict[str, float] = {}
        for row in rows:
            code = normalize_code(row.get("code"))
            history = self._history_for(code)
            value = _same_cycle_return(history, entry_date, exit_date)
            if value is not None:
                result[code] = value
        self._cycle_returns[key] = result
        return result

    def _history_for(self, code: str) -> pd.DataFrame:
        if code in self._history:
            return self._history[code]
        try:
            frame = self.provider.get_history(code, days=180)
        except Exception:
            frame = pd.DataFrame()
        self._history[code] = frame if frame is not None else pd.DataFrame()
        return self._history[code]

    @staticmethod
    def _group_summary(cycle_returns, rows, predicate, method: str, group: str = "") -> Dict[str, object]:
        codes = [normalize_code(row.get("code")) for row in rows if predicate(row)]
        constituents = [
            {"code": code, "return_pct": cycle_returns[code]}
            for code in codes
            if code in cycle_returns
        ]
        values = [item["return_pct"] for item in constituents]
        expected = len(codes)
        if not values:
            return {
                "status": "unknown",
                "return_pct": None,
                "sample_count": 0,
                "expected_count": expected,
                "coverage_pct": 0.0,
                "method": method,
                "group": group,
                "constituents": [],
            }
        return {
            "status": "ok" if len(values) == expected else "partial",
            "return_pct": round(sum(values) / len(values), 4),
            "sample_count": len(values),
            "expected_count": expected,
            "coverage_pct": round(len(values) / max(1, expected) * 100.0, 2),
            "method": method,
            "group": group,
            "constituents": constituents,
        }


def _same_cycle_return(history: pd.DataFrame, entry_date: str, exit_date: str):
    if history is None or history.empty or "trade_date" not in history.columns:
        return None
    frame = rename_known_columns(history.copy())
    if "price" not in frame.columns:
        return None
    frame["_date"] = frame["trade_date"].map(_date_key)
    frame = frame[(frame["_date"] >= entry_date) & (frame["_date"] <= exit_date)].sort_values("_date")
    if frame.empty or frame.iloc[0]["_date"] != entry_date:
        return None
    entry = coerce_number(frame.iloc[0].get("open")) or coerce_number(frame.iloc[0].get("price"))
    exit_price = coerce_number(frame.iloc[-1].get("price"))
    if entry <= 0 or exit_price <= 0:
        return None
    return round((exit_price / entry - 1.0) * 100.0, 4)


def _unknown_benchmarks(period: Dict[str, object], reason: str) -> Dict[str, object]:
    value = {
        "status": "unknown",
        "return_pct": None,
        "sample_count": 0,
        "expected_count": 0,
        "coverage_pct": 0.0,
        "reason": reason,
        "constituents": [],
    }
    return {"period": period, "market": dict(value), "industry": dict(value), "style": dict(value)}


def _candidate_value(rows: List[Dict[str, object]], code: str, field: str) -> str:
    for row in rows:
        if normalize_code(row.get("code")) == code:
            return str(row.get(field) or "")
    return ""


def _raw_signal(signal) -> Dict[str, object]:
    raw = _mapping_get(signal, "raw_json", "")
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        import json

        loaded = json.loads(raw)
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _mapping_get(row, key: str, default=None):
    if hasattr(row, "get"):
        return row.get(key, default)
    try:
        return row[key]
    except Exception:
        return default


def _date_key(value: object) -> str:
    return str(value or "").strip()[:10].replace("-", "")
