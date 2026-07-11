import json
import threading
from datetime import datetime, time as clock_time
from typing import Dict, List, Tuple

from . import config
from .deepseek.budget_policy import BudgetPolicy
from .normalization import coerce_number, normalize_code
from .runtime_json import atomic_write_json


_STATE_LOCK = threading.Lock()
_BUDGET_POLICY = BudgetPolicy()

_REUSED_ROW_FIELDS = (
    "local_rank",
    "deepseek_covered",
    "deepseek_blend_alpha",
    "blend_alpha",
    "deepseek_rule_penalty",
    "deepseek_rules_matched",
    "deepseek_score",
    "tomorrow_up_score",
    "deepseek_horizon_score",
    "deepseek_action",
    "deepseek_veto",
    "deepseek_penalty",
    "deepseek_reason",
    "deepseek_risk_flags",
    "deepseek_profit_flags",
    "deepseek_catalyst_score",
    "deepseek_theme_truth_score",
    "deepseek_event_risk_score",
    "deepseek_event_score",
    "deepseek_event_bonus",
    "deepseek_event_penalty",
    "deepseek_event_type",
    "deepseek_sentiment",
    "deepseek_catalyst_strength",
    "deepseek_time_sensitivity",
    "deepseek_already_priced_in",
    "deepseek_rank_score",
    "rerank_source",
)


def scheduled_deepseek_decision(
    strategy_name: str,
    rows: List[Dict[str, object]],
    now: datetime = None,
) -> Dict[str, object]:
    now = now or datetime.now()
    if not getattr(config, "DEEPSEEK_SCHEDULE_ENABLED", True):
        return {"enabled": False, "allow_call": True, "status": "schedule_disabled"}
    targets = _scheduled_strategies()
    if strategy_name not in targets:
        return {
            "enabled": True,
            "allow_call": False,
            "reuse": False,
            "status": "strategy_local_only",
            "strategy": strategy_name,
        }
    if not rows:
        return {"enabled": True, "allow_call": False, "reuse": False, "status": "empty"}

    profile = _schedule_profile(now)
    signature = _candidate_signature(rows)
    with _STATE_LOCK:
        state = _load_daily_state(now)
        latest = (state.get("latest") or {}).get(strategy_name)
        if not profile.get("allow_window"):
            return _reuse_decision(profile.get("status", "outside_window"), latest, profile)
        if profile.get("on_demand"):
            latest_is_late = isinstance(latest, dict) and str(latest.get("slot") or "").startswith("late:")
            if latest_is_late and signature == latest.get("signature"):
                return _reuse_decision("no_material_change", latest, profile)
            if latest_is_late and _seconds_since(latest.get("saved_at"), now) < max(
                0, int(getattr(config, "DEEPSEEK_LATE_MIN_INTERVAL_SECONDS", 300))
            ):
                return _reuse_decision("late_debounced", latest, profile)
            profile["model_tier"] = "pro" if _needs_pro_review(rows) else "base"
            profile["review_limit"] = max(
                1,
                int(
                    getattr(
                        config,
                        "DEEPSEEK_LATE_PRO_REVIEW_LIMIT"
                        if profile["model_tier"] == "pro"
                        else "DEEPSEEK_LATE_FLASH_REVIEW_LIMIT",
                        4 if profile["model_tier"] == "pro" else 6,
                    )
                ),
            )
            profile["slot"] = "late:{}".format(int(state.get("late_call_count") or 0) + 1)
            slot_key = "{}:{}".format(strategy_name, profile["slot"])
        else:
            slot_key = "{}:{}".format(strategy_name, profile["slot"])
            if slot_key in (state.get("slots") or {}):
                return _reuse_decision("slot_reused", latest, profile)
        call_cap = max(0, int(getattr(config, "DEEPSEEK_DAILY_CALL_CAP", 11)))
        pro_cap = max(0, int(getattr(config, "DEEPSEEK_DAILY_PRO_CALL_CAP", 1)))
        if int(state.get("call_count") or 0) >= call_cap:
            return _reuse_decision("daily_call_cap", latest, profile)
        if profile.get("model_tier") == "pro" and int(state.get("pro_call_count") or 0) >= pro_cap:
            profile["model_tier"] = "base"
            profile["review_limit"] = max(
                1,
                int(getattr(config, "DEEPSEEK_LATE_FLASH_REVIEW_LIMIT", 6)),
            )

        state.setdefault("slots", {})[slot_key] = {
            "reserved_at": now.isoformat(timespec="seconds"),
            "model_tier": profile["model_tier"],
            "signature": signature,
        }
        state["call_count"] = int(state.get("call_count") or 0) + 1
        if profile.get("on_demand"):
            state["late_call_count"] = int(state.get("late_call_count") or 0) + 1
        if profile.get("model_tier") == "pro":
            state["pro_call_count"] = int(state.get("pro_call_count") or 0) + 1
        _write_state(state)
    return {
        "enabled": True,
        "allow_call": True,
        "reuse": False,
        "status": "slot_reserved",
        "strategy": strategy_name,
        "signature": signature,
        **profile,
    }


def save_scheduled_deepseek_result(
    strategy_name: str,
    rows: List[Dict[str, object]],
    meta: Dict[str, object],
    decision: Dict[str, object],
    now: datetime = None,
) -> None:
    if not decision.get("enabled") or not decision.get("allow_call"):
        return
    now = now or datetime.now()
    with _STATE_LOCK:
        state = _load_daily_state(now)
        state.setdefault("latest", {})[strategy_name] = {
            "saved_at": now.isoformat(timespec="seconds"),
            "slot": decision.get("slot", ""),
            "model_tier": decision.get("model_tier", "base"),
            "signature": decision.get("signature") or _candidate_signature(rows),
            "rows": list(rows or []),
            "meta": dict(meta or {}),
        }
        usage = meta.get("usage") if isinstance(meta, dict) else {}
        if isinstance(usage, dict):
            totals = state.setdefault("usage", {})
            for key in (
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "prompt_cache_hit_tokens",
                "prompt_cache_miss_tokens",
            ):
                totals[key] = int(totals.get(key) or 0) + int(usage.get(key) or 0)
        _write_state(state)


def reuse_scheduled_deepseek_result(
    strategy_name: str,
    rows: List[Dict[str, object]],
    decision: Dict[str, object],
    now: datetime = None,
) -> Tuple[List[Dict[str, object]], Dict[str, object]]:
    now = now or datetime.now()
    with _STATE_LOCK:
        state = _load_daily_state(now)
        latest = (state.get("latest") or {}).get(strategy_name)
    if not decision.get("reuse") or not isinstance(latest, dict):
        return list(rows or []), _schedule_meta(strategy_name, decision, reused=False)

    cached_rows = list(latest.get("rows") or [])
    cached_by_code = {normalize_code(row.get("code")): row for row in cached_rows if normalize_code(row.get("code"))}
    cached_order = {normalize_code(row.get("code")): index for index, row in enumerate(cached_rows)}
    cached_meta = dict(latest.get("meta") or {})
    filtered_codes = {normalize_code(code) for code in cached_meta.get("filtered_codes") or []}
    merged = []
    for index, row in enumerate(rows or []):
        code = normalize_code(row.get("code"))
        if code in filtered_codes:
            continue
        item = dict(row)
        cached = cached_by_code.get(code)
        if cached:
            for key in _REUSED_ROW_FIELDS:
                if key in cached:
                    item[key] = cached[key]
            if item.get("tier") == "backup_pool" or item.get("observation_mode") == "intraday_provisional":
                if item.get("deepseek_action") != "avoid":
                    item["deepseek_action"] = "watch"
        item["_schedule_order"] = cached_order.get(code, len(cached_rows) + index)
        merged.append(item)
    merged.sort(
        key=lambda item: (
            int(item.pop("_schedule_order", len(cached_rows))),
            -coerce_number(item.get("deepseek_rank_score"), coerce_number(item.get("score"))),
        )
    )
    for rank, row in enumerate(merged, start=1):
        row["rank"] = rank
    cached_meta.update(_schedule_meta(strategy_name, decision, reused=True))
    cached_meta["source"] = "deepseek_schedule_cache"
    cached_meta["cached_at"] = latest.get("saved_at")
    return merged, cached_meta


def deepseek_schedule_status(now: datetime = None) -> Dict[str, object]:
    now = now or datetime.now()
    with _STATE_LOCK:
        state = _load_daily_state(now)
    latest = {}
    for strategy, item in (state.get("latest") or {}).items():
        if not isinstance(item, dict):
            continue
        latest[strategy] = {
            "saved_at": item.get("saved_at", ""),
            "slot": item.get("slot", ""),
            "model_tier": item.get("model_tier", ""),
            "candidate_count": len(item.get("rows") or []),
        }
    return {
        "enabled": bool(getattr(config, "DEEPSEEK_SCHEDULE_ENABLED", True)),
        "date": state.get("date", now.date().isoformat()),
        "call_count": int(state.get("call_count") or 0),
        "call_cap": max(0, int(getattr(config, "DEEPSEEK_DAILY_CALL_CAP", 11))),
        "pro_call_count": int(state.get("pro_call_count") or 0),
        "pro_call_cap": max(0, int(getattr(config, "DEEPSEEK_DAILY_PRO_CALL_CAP", 1))),
        "late_call_count": int(state.get("late_call_count") or 0),
        "used_slots": sorted((state.get("slots") or {}).keys()),
        "usage": dict(state.get("usage") or {}),
        "latest": latest,
    }


def _schedule_profile(now: datetime) -> Dict[str, object]:
    if now.weekday() >= 5:
        return {"allow_window": False, "status": "non_trading_day"}
    current = now.time()
    if clock_time(9, 30) <= current < clock_time(11, 30):
        slot = _bucket_slot(now, 30)
        return _profile(slot, "base", getattr(config, "DEEPSEEK_EARLY_REVIEW_LIMIT", 4))
    if clock_time(13, 0) <= current < clock_time(14, 30):
        slot = _bucket_slot(now, 30)
        return _profile(slot, "base", getattr(config, "DEEPSEEK_EARLY_REVIEW_LIMIT", 4))
    if clock_time(14, 30) <= current < clock_time(15, 0):
        profile = _profile("late", "base", getattr(config, "DEEPSEEK_LATE_FLASH_REVIEW_LIMIT", 6))
        profile["on_demand"] = True
        return profile
    return {"allow_window": False, "status": "outside_trading_window"}


def _bucket_slot(now: datetime, bucket_minutes: int) -> str:
    minutes = max(1, int(bucket_minutes or 1))
    bucket_minute = (now.minute // minutes) * minutes
    return "{:02d}:{:02d}".format(now.hour, bucket_minute)


def _profile(slot: str, model_tier: str, review_limit: int) -> Dict[str, object]:
    return {
        "allow_window": True,
        "slot": slot,
        "model_tier": model_tier,
        "review_limit": max(1, int(review_limit or 1)),
        "on_demand": False,
    }


def _reuse_decision(status: str, latest: object, profile: Dict[str, object]) -> Dict[str, object]:
    return {
        "enabled": True,
        "allow_call": False,
        "reuse": isinstance(latest, dict),
        "status": status,
        **profile,
    }


def _scheduled_strategies() -> set:
    raw = str(getattr(config, "DEEPSEEK_SCHEDULE_STRATEGIES", "tomorrow_picks") or "")
    return {item.strip() for item in raw.replace("，", ",").split(",") if item.strip()}


def _candidate_signature(rows: List[Dict[str, object]]) -> List[object]:
    signature = []
    for row in (rows or [])[:3]:
        signature.append(
            [
                normalize_code(row.get("code")),
                int(round(coerce_number(row.get("score"), 0.0))),
                sorted(str(item) for item in (row.get("event_risk_flags") or row.get("risk_words") or [])[:3]),
            ]
        )
    return signature


def _needs_pro_review(rows: List[Dict[str, object]]) -> bool:
    return _BUDGET_POLICY.needs_pro_review(rows)


def _seconds_since(value: object, now: datetime) -> float:
    try:
        saved_at = datetime.fromisoformat(str(value or ""))
        return max(0.0, (now - saved_at).total_seconds())
    except Exception:
        return float("inf")


def _load_daily_state(now: datetime) -> Dict[str, object]:
    path = str(getattr(config, "DEEPSEEK_SCHEDULE_STATE_PATH", ".runtime/deepseek_schedule.json") or "")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except Exception:
        state = {}
    today = now.date().isoformat()
    if not isinstance(state, dict) or state.get("date") != today:
        return {
            "date": today,
            "call_count": 0,
            "pro_call_count": 0,
            "late_call_count": 0,
            "slots": {},
            "latest": {},
            "usage": {},
        }
    return state


def _write_state(state: Dict[str, object]) -> None:
    path = str(getattr(config, "DEEPSEEK_SCHEDULE_STATE_PATH", ".runtime/deepseek_schedule.json") or "")
    if not path:
        return
    try:
        atomic_write_json(path, state, ensure_ascii=False, separators=(",", ":"), default=_json_value)
    except Exception:
        return


def _schedule_meta(strategy_name: str, decision: Dict[str, object], reused: bool) -> Dict[str, object]:
    return {
        "enabled": True,
        "status": "schedule_cache_hit" if reused else decision.get("status", "schedule_deferred"),
        "strategy": strategy_name,
        "schedule": {
            "status": decision.get("status", ""),
            "slot": decision.get("slot", ""),
            "model_tier": decision.get("model_tier", ""),
            "reused": reused,
        },
    }


def _json_value(value: object):
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return str(value)
