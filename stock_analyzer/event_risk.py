import json
import os
import time
from datetime import datetime, timedelta
from typing import Dict, Iterable, List

import pandas as pd

from . import config
from .normalization import coerce_number, normalize_code
from .runtime_json import atomic_write_json


def load_event_risk(provider=None, force: bool = False) -> Dict[str, object]:
    if not getattr(config, "ENABLE_EVENT_RISK", False):
        return {"enabled": False, "status": "disabled", "items": {}, "generated_at": ""}
    cached = _load_cache()
    if cached and not force:
        return {**cached, "enabled": True, "status": cached.get("status", "cached")}
    if provider is None:
        return {"enabled": True, "status": "no_provider", "items": {}, "generated_at": ""}
    try:
        items = build_event_risk_map(
            unlocks=provider.get_share_unlock_events(),
            pledges=provider.get_pledge_risk(),
            reductions=provider.get_reduction_plans(),
            reports=provider.get_financial_calendar(),
        )
        payload = {
            "enabled": True,
            "status": "ok",
            "items": items,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        }
        _save_cache(payload)
        return payload
    except Exception as exc:
        return {"enabled": True, "status": "error", "error": str(exc), "items": {}, "generated_at": ""}


def build_event_risk_map(
    unlocks: Iterable[Dict[str, object]] = (),
    pledges: Iterable[Dict[str, object]] = (),
    reductions: Iterable[Dict[str, object]] = (),
    reports: Iterable[Dict[str, object]] = (),
) -> Dict[str, Dict[str, object]]:
    risks: Dict[str, Dict[str, object]] = {}
    for row in unlocks or []:
        code = normalize_code(_first(row, "code", "ts_code", "股票代码", "证券代码"))
        if not code:
            continue
        ratio = coerce_number(_first(row, "unlock_ratio", "float_ratio", "解禁比例", "解禁占比"))
        days = _days_until(_first(row, "date", "unlock_date", "解禁日期"))
        announcement_time = _first(row, "announcement_time", "announce_date", "公告日期", "公告发布时间")
        if days is None or days < 0 or days > int(getattr(config, "EVENT_RISK_LOOKAHEAD_DAYS", 30)):
            continue
        warn = coerce_number(getattr(config, "EVENT_RISK_UNLOCK_WARN_RATIO", 3.0), 3.0)
        high = coerce_number(getattr(config, "EVENT_RISK_UNLOCK_HIGH_RATIO", 8.0), 8.0)
        if ratio >= high:
            _add_flag(risks, code, "大额解禁", "high", getattr(config, "EVENT_RISK_PENALTY_HIGH", 18.0), ratio, days, announcement_time)
        elif ratio >= warn:
            _add_flag(risks, code, "限售解禁", "medium", getattr(config, "EVENT_RISK_PENALTY_MEDIUM", 9.0), ratio, days, announcement_time)

    for row in pledges or []:
        code = normalize_code(_first(row, "code", "ts_code", "股票代码", "证券代码"))
        if not code:
            continue
        ratio = coerce_number(_first(row, "pledge_ratio", "pledge_ratio_pct", "质押比例", "质押占比"))
        warn = coerce_number(getattr(config, "EVENT_RISK_PLEDGE_WARN_RATIO", 35.0), 35.0)
        high = coerce_number(getattr(config, "EVENT_RISK_PLEDGE_HIGH_RATIO", 55.0), 55.0)
        if ratio >= high:
            _add_flag(risks, code, "高质押", "high", getattr(config, "EVENT_RISK_PENALTY_HIGH", 18.0), ratio, None, _first(row, "announcement_time", "announce_date", "公告日期", "公告发布时间"))
        elif ratio >= warn:
            _add_flag(risks, code, "质押偏高", "medium", getattr(config, "EVENT_RISK_PENALTY_MEDIUM", 9.0), ratio, None, _first(row, "announcement_time", "announce_date", "公告日期", "公告发布时间"))

    for row in reductions or []:
        code = normalize_code(_first(row, "code", "ts_code", "股票代码", "证券代码"))
        date_value = _first(row, "date", "announce_date", "公告日期", "变动日期")
        days_since = _days_since(date_value)
        days_until = _days_until(date_value)
        lookback = int(getattr(config, "EVENT_RISK_REDUCTION_LOOKBACK_DAYS", 120))
        recent = days_since is not None and 0 <= days_since <= lookback
        upcoming = days_until is not None and 0 <= days_until <= int(getattr(config, "EVENT_RISK_LOOKAHEAD_DAYS", 30))
        if code and (recent or upcoming):
            _add_flag(risks, code, "减持计划", "medium", getattr(config, "EVENT_RISK_PENALTY_MEDIUM", 9.0), 0.0, days_until, date_value)

    report_window = int(getattr(config, "EVENT_RISK_REPORT_WINDOW_DAYS", 5))
    for row in reports or []:
        code = normalize_code(_first(row, "code", "ts_code", "股票代码", "证券代码"))
        days = _days_until(_first(row, "date", "report_date", "预约披露日期", "公告日期"))
        if code and days is not None and abs(days) <= report_window:
            _add_flag(risks, code, "财报窗口", "low", getattr(config, "EVENT_RISK_PENALTY_LOW", 4.0), 0.0, days, _first(row, "announcement_time", "announce_date", "公告日期", "公告发布时间"))

    hard_threshold = coerce_number(getattr(config, "EVENT_RISK_HARD_PENALTY", 24.0), 24.0)
    for item in risks.values():
        penalty = min(coerce_number(getattr(config, "EVENT_RISK_MAX_PENALTY", 30.0), 30.0), item["penalty"])
        high_count = sum(1 for flag in item["flags"] if flag.get("level") == "high")
        item["penalty"] = round(penalty, 2)
        item["level"] = "high" if penalty >= hard_threshold or high_count >= 2 else "medium" if penalty >= 8 else "low"
        item["hard_exclude"] = item["level"] == "high" and bool(getattr(config, "EVENT_RISK_HARD_FILTER", False))
    return risks


def attach_event_risk(df: pd.DataFrame, payload: Dict[str, object]) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    result = df.copy()
    items = (payload or {}).get("items") or {}
    result["event_risk_penalty"] = result["code"].map(lambda code: coerce_number(items.get(normalize_code(code), {}).get("penalty")))
    result["event_risk_level"] = result["code"].map(lambda code: str(items.get(normalize_code(code), {}).get("level", "none")))
    result["event_risk_flags"] = result["code"].map(lambda code: items.get(normalize_code(code), {}).get("flags", []))
    result["event_risk_status"] = str((payload or {}).get("status", "missing"))
    result["event_risk_hard_exclude"] = result["code"].map(lambda code: _is_hard_excluded(items.get(normalize_code(code), {})))
    if bool(getattr(config, "EVENT_RISK_HARD_FILTER", False)):
        result = result[~result["event_risk_hard_exclude"]].copy()
    return result.reset_index(drop=True)


def row_event_risk(row) -> Dict[str, object]:
    flags = row.get("event_risk_flags", []) if hasattr(row, "get") else []
    if not isinstance(flags, list):
        flags = []
    return {
        "penalty": coerce_number(row.get("event_risk_penalty") if hasattr(row, "get") else 0.0),
        "level": str(row.get("event_risk_level", "none") if hasattr(row, "get") else "none"),
        "flags": flags,
        "status": str(row.get("event_risk_status", "missing") if hasattr(row, "get") else "missing"),
    }


def _is_hard_excluded(item: Dict[str, object]) -> bool:
    if not item:
        return False
    if bool(item.get("hard_exclude")):
        return True
    if not bool(getattr(config, "EVENT_RISK_HARD_FILTER", False)):
        return False
    return str(item.get("level", "none")) == "high"


def _add_flag(
    risks: Dict[str, Dict[str, object]],
    code: str,
    label: str,
    level: str,
    penalty: float,
    value: float,
    days: int,
    announcement_time=None,
) -> None:
    item = risks.setdefault(code, {"code": code, "flags": [], "penalty": 0.0})
    item["flags"].append(
        {
            "label": label,
            "level": level,
            "value": round(coerce_number(value), 2),
            "days": days,
            "penalty": round(coerce_number(penalty), 2),
            "announcement_time": str(announcement_time or ""),
        }
    )
    item["penalty"] += coerce_number(penalty)


def _first(row: Dict[str, object], *keys: str):
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None


def _days_until(value) -> int:
    if value in (None, ""):
        return None
    text = str(value).replace("-", "")[:8]
    try:
        date = datetime.strptime(text, "%Y%m%d").date()
    except Exception:
        return None
    return (date - datetime.now().date()).days


def _days_since(value) -> int:
    days = _days_until(value)
    if days is None:
        return None
    return -days


def _load_cache() -> Dict[str, object]:
    path = getattr(config, "EVENT_RISK_CACHE_PATH", ".runtime/event_risk.json")
    try:
        if not os.path.exists(path):
            return {}
        max_age = int(getattr(config, "EVENT_RISK_CACHE_HOURS", 24)) * 3600
        if time.time() - os.path.getmtime(path) > max_age:
            return {}
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _save_cache(payload: Dict[str, object]) -> None:
    path = getattr(config, "EVENT_RISK_CACHE_PATH", ".runtime/event_risk.json")
    try:
        atomic_write_json(path, payload, ensure_ascii=False, indent=2)
    except Exception:
        return
