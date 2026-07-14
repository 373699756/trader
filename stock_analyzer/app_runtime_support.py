from typing import Dict, List

from . import config


def risk_blacklist_summary(payload: Dict[str, object]) -> Dict[str, object]:
    payload = payload or {}
    return {
        "enabled": bool(getattr(config, "ENABLE_RISK_BLACKLIST", True)),
        "hard_filter": bool(getattr(config, "RISK_BLACKLIST_HARD_FILTER", True)),
        "status": payload.get("status", "missing"),
        "item_count": len((payload.get("items") or {})),
        "sources": payload.get("sources", []),
        "error_count": len(payload.get("errors") or []),
    }


def skipped_deepseek_meta(
    strategy_name: str,
    *,
    reason: str = "No point-in-time DeepSeek features were requested.",
    status: str = "local_only",
) -> Dict[str, object]:
    return {
        "enabled": bool(getattr(config, "ENABLE_DEEPSEEK_FEATURES", True)),
        "status": status,
        "strategy": strategy_name,
        "production_applied": False,
        "reason": reason,
    }


def finalize_deepseek_meta(
    meta: Dict[str, object],
    rows: List[Dict[str, object]],
    deepseek_meta: Dict[str, object],
) -> None:
    meta["deepseek"] = _public_deepseek_meta(deepseek_meta)
    meta["display_count"] = len(rows)
    sync_tomorrow_tier_meta(meta, rows)


def sync_tomorrow_tier_meta(
    meta: Dict[str, object],
    rows: List[Dict[str, object]],
) -> None:
    if not isinstance(meta, dict):
        return
    strategy_version = str(meta.get("strategy_version") or "")
    if not strategy_version.startswith("tomorrow_picks") and meta.get("strategy_label") not in {
        "明日优先",
        "明天推荐",
    }:
        return
    primary_count = sum(1 for row in rows or [] if row.get("tier") == "primary_watch")
    meta["display_count"] = len(rows or [])
    meta["primary_watch_count"] = primary_count
    meta["backup_watch_count"] = max(0, len(rows or []) - primary_count)


def _public_deepseek_meta(deepseek_meta: Dict[str, object]) -> Dict[str, object]:
    item = dict(deepseek_meta or {})
    item.pop("filtered_rows", None)
    item.pop("shadow_filtered_rows", None)
    return item
