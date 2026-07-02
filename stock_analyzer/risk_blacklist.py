import csv
import json
import os
from datetime import datetime
from typing import Dict, List

import pandas as pd

from . import config
from .normalization import coerce_number, normalize_code


_BLACKLIST_CACHE = {"key": None, "payload": None}


def load_risk_blacklist() -> Dict[str, object]:
    if not getattr(config, "ENABLE_RISK_BLACKLIST", True):
        return {"enabled": False, "status": "disabled", "items": {}, "generated_at": ""}

    json_path = getattr(config, "RISK_BLACKLIST_PATH", ".runtime/risk_blacklist.json")
    csv_path = getattr(config, "RISK_BLACKLIST_CSV_PATH", ".runtime/risk_blacklist.csv")
    cache_key = _cache_key(json_path, csv_path)
    if _BLACKLIST_CACHE["key"] == cache_key and _BLACKLIST_CACHE["payload"] is not None:
        return _BLACKLIST_CACHE["payload"]

    items: Dict[str, Dict[str, object]] = {}
    sources: List[str] = []
    errors: List[str] = []

    json_items, json_error = _load_json_items(json_path)
    if json_error:
        errors.append(json_error)
    elif json_items:
        _merge_items(items, json_items)
        sources.append(json_path)

    csv_items, csv_error = _load_csv_items(csv_path)
    if csv_error:
        errors.append(csv_error)
    elif csv_items:
        _merge_items(items, csv_items)
        sources.append(csv_path)

    status = "ok" if items else "empty"
    if errors and not items:
        status = "error"
    elif errors:
        status = "partial"

    payload = {
        "enabled": True,
        "status": status,
        "items": items,
        "sources": sources,
        "errors": errors,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    _BLACKLIST_CACHE["key"] = cache_key
    _BLACKLIST_CACHE["payload"] = payload
    return payload


def attach_risk_blacklist(df: pd.DataFrame, payload: Dict[str, object]) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    if "code" not in df.columns:
        return df.copy()
    result = df.copy()
    items = (payload or {}).get("items") or {}
    result["blacklist_risk_penalty"] = result["code"].map(lambda code: coerce_number(items.get(normalize_code(code), {}).get("penalty")))
    result["blacklist_risk_level"] = result["code"].map(lambda code: str(items.get(normalize_code(code), {}).get("level", "none")))
    result["blacklist_risk_flags"] = result["code"].map(lambda code: items.get(normalize_code(code), {}).get("flags", []))
    result["blacklist_risk_status"] = str((payload or {}).get("status", "missing"))
    result["blacklist_hard_exclude"] = result["code"].map(lambda code: _is_hard_excluded(items.get(normalize_code(code), {})))
    if bool(getattr(config, "RISK_BLACKLIST_HARD_FILTER", True)):
        result = result[~result["blacklist_hard_exclude"]].copy()
    return result.reset_index(drop=True)


def row_blacklist_risk(row) -> Dict[str, object]:
    flags = row.get("blacklist_risk_flags", []) if hasattr(row, "get") else []
    if not isinstance(flags, list):
        flags = []
    return {
        "penalty": coerce_number(row.get("blacklist_risk_penalty") if hasattr(row, "get") else 0.0),
        "level": str(row.get("blacklist_risk_level", "none") if hasattr(row, "get") else "none"),
        "flags": flags,
        "hard_exclude": bool(row.get("blacklist_hard_exclude", False) if hasattr(row, "get") else False),
        "status": str(row.get("blacklist_risk_status", "missing") if hasattr(row, "get") else "missing"),
    }


def blacklist_risk_for_code(code: str, payload: Dict[str, object] = None) -> Dict[str, object]:
    payload = payload or load_risk_blacklist()
    item = ((payload or {}).get("items") or {}).get(normalize_code(code), {})
    if not item:
        return {"penalty": 0.0, "level": "none", "flags": [], "hard_exclude": False, "status": (payload or {}).get("status", "missing")}
    return {
        "penalty": coerce_number(item.get("penalty")),
        "level": str(item.get("level", "none")),
        "flags": list(item.get("flags") or []),
        "hard_exclude": _is_hard_excluded(item),
        "status": str((payload or {}).get("status", "missing")),
    }


def _load_json_items(path: str) -> tuple:
    if not path or not os.path.exists(path):
        return {}, ""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        raw_items = payload.get("items", payload) if isinstance(payload, dict) else payload
        return _normalize_items(raw_items, source=path), ""
    except Exception as exc:
        return {}, "{} 加载失败: {}".format(path, exc)


def _load_csv_items(path: str) -> tuple:
    if not path or not os.path.exists(path):
        return {}, ""
    rows = []
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            rows.extend(reader)
        return _normalize_items(rows, source=path), ""
    except Exception as exc:
        return {}, "{} 加载失败: {}".format(path, exc)


def _normalize_items(raw_items, source: str = "") -> Dict[str, Dict[str, object]]:
    if isinstance(raw_items, dict):
        iterable = []
        for code, value in raw_items.items():
            if isinstance(value, dict):
                iterable.append({"code": code, **value})
            else:
                iterable.append({"code": code, "reason": str(value)})
    else:
        iterable = list(raw_items or [])

    items: Dict[str, Dict[str, object]] = {}
    for row in iterable:
        if not isinstance(row, dict):
            continue
        raw_code = _first(row, "code", "ts_code", "股票代码", "证券代码")
        if raw_code in (None, ""):
            continue
        code = normalize_code(raw_code)
        if _is_expired(_first(row, "expires_at", "expire_at", "有效期至", "到期日")):
            continue
        level = str(_first(row, "level", "severity", "风险等级") or "high").lower()
        reason = str(_first(row, "reason", "label", "风险原因", "原因") or "历史重大负面风险")
        category = str(_first(row, "category", "type", "风险类型", "类型") or "black_history")
        penalty = coerce_number(_first(row, "penalty", "扣分"), _default_penalty(level))
        hard_exclude = _bool_value(_first(row, "hard_exclude", "hard", "硬过滤"), default=_level_is_high(level))
        flag = {
            "label": reason,
            "category": category,
            "level": level,
            "source": str(_first(row, "source", "来源") or source),
            "date": str(_first(row, "date", "announce_date", "公告日期") or ""),
        }
        item = items.setdefault(
            code,
            {
                "code": code,
                "name": str(_first(row, "name", "股票简称", "名称") or ""),
                "flags": [],
                "penalty": 0.0,
                "level": level,
                "hard_exclude": False,
            },
        )
        item["flags"].append(flag)
        item["penalty"] += penalty
        item["level"] = _max_level(item.get("level", "low"), level)
        item["hard_exclude"] = bool(item.get("hard_exclude")) or hard_exclude
    for item in items.values():
        item["penalty"] = round(coerce_number(item.get("penalty")), 2)
        if _level_is_high(item.get("level")):
            item["hard_exclude"] = True if getattr(config, "RISK_BLACKLIST_HARD_FILTER", True) else bool(item.get("hard_exclude"))
    return items


def _merge_items(target: Dict[str, Dict[str, object]], incoming: Dict[str, Dict[str, object]]) -> None:
    for code, item in incoming.items():
        current = target.setdefault(
            code,
            {
                "code": code,
                "name": item.get("name", ""),
                "flags": [],
                "penalty": 0.0,
                "level": "low",
                "hard_exclude": False,
            },
        )
        current["flags"].extend(item.get("flags") or [])
        current["penalty"] = round(coerce_number(current.get("penalty")) + coerce_number(item.get("penalty")), 2)
        current["level"] = _max_level(current.get("level", "low"), item.get("level", "low"))
        current["hard_exclude"] = bool(current.get("hard_exclude")) or bool(item.get("hard_exclude"))
        if not current.get("name") and item.get("name"):
            current["name"] = item.get("name")


def _is_hard_excluded(item: Dict[str, object]) -> bool:
    if not item or not bool(getattr(config, "RISK_BLACKLIST_HARD_FILTER", True)):
        return False
    return bool(item.get("hard_exclude")) or _level_is_high(item.get("level"))


def _cache_key(json_path: str, csv_path: str):
    parts = [
        bool(getattr(config, "RISK_BLACKLIST_HARD_FILTER", True)),
        tuple(getattr(config, "RISK_BLACKLIST_HIGH_LEVELS", ("high", "critical"))),
    ]
    for path in (json_path, csv_path):
        if not path or not os.path.exists(path):
            parts.append((path, None, None))
            continue
        try:
            stat = os.stat(path)
            parts.append((path, stat.st_mtime_ns, stat.st_size))
        except OSError:
            parts.append((path, None, None))
    return tuple(parts)


def _level_is_high(level: object) -> bool:
    return str(level or "").lower() in set(getattr(config, "RISK_BLACKLIST_HIGH_LEVELS", ("high", "critical")))


def _max_level(left: object, right: object) -> str:
    order = {"none": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
    left_text = str(left or "none").lower()
    right_text = str(right or "none").lower()
    return left_text if order.get(left_text, 0) >= order.get(right_text, 0) else right_text


def _default_penalty(level: str) -> float:
    if str(level).lower() == "critical":
        return 100.0
    if str(level).lower() == "high":
        return 80.0
    if str(level).lower() == "medium":
        return 35.0
    return 15.0


def _bool_value(value, default: bool = False) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on", "是", "硬过滤")


def _is_expired(value) -> bool:
    if value in (None, ""):
        return False
    try:
        expires = pd.to_datetime(str(value), errors="coerce")
    except Exception:
        return False
    if pd.isna(expires):
        return False
    return expires.date() < datetime.now().date()


def _first(row: Dict[str, object], *keys: str):
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None
