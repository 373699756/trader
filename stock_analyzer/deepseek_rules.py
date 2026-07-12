from __future__ import annotations

import json
import os
from typing import Dict, List, Tuple

from . import config
from .normalization import coerce_number


_RULE_CACHE = {"path": None, "mtime": None, "rules": {}}


def load_deepseek_rules() -> Dict[str, List[Dict[str, object]]]:
    path = str(getattr(config, "WEIGHTS_OVERRIDE_PATH", ".runtime/weights.json") or "")
    if not path or not os.path.exists(path):
        _RULE_CACHE.update({"path": path, "mtime": None, "rules": {}})
        return {}
    try:
        mtime = os.path.getmtime(path)
    except Exception:
        return {}
    if _RULE_CACHE.get("path") == path and _RULE_CACHE.get("mtime") == mtime:
        return _RULE_CACHE.get("rules") or {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        raw_rules = payload.get("deepseek_rules") if isinstance(payload, dict) else {}
        rules = _normalize_rules(raw_rules)
    except Exception:
        rules = {}
    _RULE_CACHE.update({"path": path, "mtime": mtime, "rules": rules})
    return rules


def rule_penalty_for_row(strategy: str, row: Dict[str, object]) -> Tuple[float, List[Dict[str, object]]]:
    rules = load_deepseek_rules().get(strategy) or []
    if not rules:
        return 0.0, []
    matched: List[Dict[str, object]] = []
    penalty = 0.0
    for rule in rules:
        if not rule_matches(row, rule):
            continue
        rule_penalty = max(0.0, coerce_number(rule.get("penalty"), 0.0))
        if rule_penalty <= 0:
            continue
        penalty += rule_penalty
        matched.append(
            {
                "field": rule.get("field"),
                "operator": rule.get("operator"),
                "threshold": rule.get("threshold"),
                "penalty": round(rule_penalty, 2),
                "reason": str(rule.get("reason") or "DeepSeek OOS规则扣分"),
            }
        )
    return round(min(penalty, 45.0), 2), matched


def apply_rule_penalty(strategy: str, row: Dict[str, object]) -> Dict[str, object]:
    if row.get("deepseek_rule_penalty") is not None and row.get("deepseek_rules_matched") is not None:
        return row
    penalty, matched = rule_penalty_for_row(strategy, row)
    if penalty <= 0:
        return row
    next_row = dict(row)
    if bool(getattr(config, "DEEPSEEK_SHADOW_ONLY", False)):
        next_row["deepseek_rule_shadow_penalty"] = penalty
        next_row["deepseek_rule_shadow_matches"] = matched
        next_row["deepseek_rule_shadow_only"] = True
        return next_row
    base_score = coerce_number(next_row.get("score"), 0.0)
    next_row["deepseek_rule_penalty"] = penalty
    next_row["deepseek_rules_matched"] = matched
    next_row["score_before_deepseek_rules"] = round(base_score, 2)
    next_row["score"] = round(max(0.0, min(100.0, base_score - penalty)), 2)
    reasons = list(next_row.get("reasons") or [])
    reason = matched[0].get("reason") if matched else "DeepSeek OOS规则扣分"
    label = "OOS规则扣分: {}".format(reason)
    if label not in reasons:
        reasons.append(label)
    next_row["reasons"] = reasons[:6]
    return next_row


def rule_matches(row: Dict[str, object], rule: Dict[str, object]) -> bool:
    field = str(rule.get("field") or "").strip()
    operator = str(rule.get("operator") or "").strip().lower()
    if not field or not operator:
        return False
    value = rule_field_value(row, field)
    threshold = rule.get("threshold")
    if value is None:
        return False
    if operator in {">", ">=", "<", "<=", "==", "=", "!="}:
        left = coerce_number(value, float("nan"))
        right = coerce_number(threshold, float("nan"))
        if left != left or right != right:
            return False
        if operator == ">":
            return left > right
        if operator == ">=":
            return left >= right
        if operator == "<":
            return left < right
        if operator == "<=":
            return left <= right
        if operator in {"==", "="}:
            return left == right
        return left != right
    if operator == "contains":
        return str(threshold) in str(value)
    if operator == "in":
        if isinstance(threshold, (list, tuple, set)):
            return str(value) in {str(item) for item in threshold}
        return str(value) in {item.strip() for item in str(threshold).split(",")}
    return False


def rule_field_value(row: Dict[str, object], field: str):
    roots = [row]
    raw = row.get("raw") if isinstance(row, dict) else None
    if isinstance(raw, dict):
        roots.append(raw)
    for root in roots:
        value = root
        found = True
        for part in field.split("."):
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                found = False
                break
        if found:
            return value
    return None


def _normalize_rules(raw_rules) -> Dict[str, List[Dict[str, object]]]:
    if not isinstance(raw_rules, dict):
        return {}
    normalized: Dict[str, List[Dict[str, object]]] = {}
    for strategy, rules in raw_rules.items():
        if isinstance(rules, dict):
            rules = [rules]
        if not isinstance(rules, list):
            continue
        clean_rules = []
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            field = str(rule.get("field") or "").strip()
            operator = str(rule.get("operator") or "").strip()
            penalty = max(0.0, coerce_number(rule.get("penalty"), 0.0))
            if not field or not operator or penalty <= 0:
                continue
            clean_rules.append(
                {
                    "field": field,
                    "operator": operator,
                    "threshold": rule.get("threshold"),
                    "penalty": min(penalty, 45.0),
                    "reason": str(rule.get("reason") or "DeepSeek OOS规则扣分"),
                }
            )
        if clean_rules:
            normalized[str(strategy)] = clean_rules
    return normalized
