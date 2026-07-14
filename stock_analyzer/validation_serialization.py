from __future__ import annotations

import json
import sqlite3
from typing import Dict

from .validation_policy import stored_or_current_trade_cost_pct


def signal_row_to_dict(row: sqlite3.Row) -> Dict[str, object]:
    item = dict(row)
    for key in ("reasons_json", "raw_json"):
        try:
            item[key.replace("_json", "")] = json.loads(item.get(key) or "[]")
        except Exception:
            item[key.replace("_json", "")] = [] if key == "reasons_json" else {}
    for key, fallback in (
        ("execution_policy_json", {}),
        ("cost_scenarios_json", {}),
        ("raw_prices_json", []),
        ("benchmark_json", {}),
    ):
        target = key.replace("_json", "")
        try:
            loaded = json.loads(item.get(key) or ("[]" if isinstance(fallback, list) else "{}"))
        except Exception:
            loaded = fallback
        item[target] = loaded if isinstance(loaded, type(fallback)) else fallback
    item["promotion_eligible"] = bool(item.get("promotion_eligible"))
    item["return_reproducible"] = bool(item.get("return_reproducible"))
    item["trade_cost_pct"] = stored_or_current_trade_cost_pct(item)
    return item


def tuning_row_to_dict(row: sqlite3.Row) -> Dict[str, object]:
    item = dict(row)
    for key in ("plan_json", "metrics_json", "deepseek_json"):
        target = key.replace("_json", "")
        try:
            item[target] = json.loads(item.get(key) or "{}")
        except Exception:
            item[target] = {}
        item.pop(key, None)
    item["can_apply"] = bool(item.get("can_apply"))
    item["shadow_mode"] = bool(item.get("shadow_mode"))
    return item


def oos_report_row_to_dict(row: sqlite3.Row) -> Dict[str, object]:
    item = dict(row)
    for key in (
        "report_json",
        "baseline_status_json",
        "validation_gate_json",
        "requirements_json",
        "experiment_audit_json",
    ):
        target = key.replace("_json", "")
        try:
            item[target] = json.loads(item.get(key) or "{}")
        except Exception:
            item[target] = {}
        item.pop(key, None)
    item["gate_blocked"] = bool(item.get("gate_blocked"))
    report = item.get("report") if isinstance(item.get("report"), dict) else {}
    if report:
        item.setdefault("summary", report.get("summary") or {})
        item.setdefault("validation_baseline", report.get("validation_baseline") or {})
        item.setdefault("validation_baseline_id", report.get("validation_baseline_id") or item.get("baseline_id"))
        item.setdefault("experiment_audit", report.get("experiment_audit") or item.get("experiment_audit"))
    return item
