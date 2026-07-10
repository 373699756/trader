from typing import Dict, List

from . import config
from .app_support import apply_tomorrow_validation_gate, attach_validation_summary
from .app_runtime_support import risk_blacklist_summary


def response_payload(
    provider_health_fn,
    research_disclaimer_fn,
    *,
    ok: bool,
    include_health: bool = True,
    include_disclaimer: bool = False,
    **payload,
) -> Dict[str, object]:
    body = {"ok": ok, **payload}
    if include_health:
        body["health"] = provider_health_fn()
    if include_disclaimer:
        body["disclaimer"] = research_disclaimer_fn()
    return body


def error_payload(
    provider_health_fn,
    research_disclaimer_fn,
    error,
    **payload,
) -> Dict[str, object]:
    return response_payload(
        provider_health_fn,
        research_disclaimer_fn,
        ok=False,
        error=str(error),
        include_disclaimer=True,
        **payload,
    )


def snapshot_fallback_payload(snapshot: Dict[str, object], error) -> Dict[str, object]:
    payload = dict(snapshot["payload"])
    payload["snapshot_fallback"] = {
        "status": "latest_recommendation_snapshot",
        "saved_at": snapshot.get("saved_at", ""),
        "age_seconds": snapshot.get("age_seconds"),
        "error": str(error),
    }
    return payload


def saved_tomorrow_fallback_payload(
    *,
    saved_rows: List[Dict[str, object]],
    top_n: int,
    market: str,
    detailed: bool,
    validation_store,
    cached_metrics_fn,
    load_risk_blacklist_fn,
    analysis_window_fn,
    provider_health_fn,
    research_disclaimer_fn,
) -> Dict[str, object]:
    attach_validation_summary(saved_rows, validation_store, "tomorrow_picks", metrics_fn=cached_metrics_fn)
    display_limit = min(
        max(0, int(top_n or 0)),
        max(0, int(getattr(config, "TOMORROW_RECOMMENDATION_DISPLAY_LIMIT", top_n or 0))),
    )
    validation_meta = {
        "gate_reason": "实时行情不可用，显示最近保存快照；不代表今日实时盘面。",
        **_saved_tomorrow_tier_counts(saved_rows),
    }
    try:
        apply_tomorrow_validation_gate(
            saved_rows,
            validation_meta,
            cached_metrics_fn("tomorrow_picks", 20),
        )
    except Exception:
        pass
    display_rows = saved_rows[:display_limit]
    tier_counts = _saved_tomorrow_tier_counts(display_rows)
    strategy_version = _saved_tomorrow_strategy_version(saved_rows)
    if not detailed:
        return response_payload(
            provider_health_fn,
            research_disclaimer_fn,
            ok=True,
            include_disclaimer=True,
            data=display_rows,
            meta={
                "generated_at": "",
                "candidate_count": len(saved_rows),
                "display_count": len(display_rows),
                "display_limit": display_limit,
                "display_cap": getattr(config, "TOMORROW_RECOMMENDATION_DISPLAY_LIMIT", display_limit),
                "primary_watch_count": tier_counts["primary_watch_count"],
                "backup_watch_count": tier_counts["backup_watch_count"],
                "top_n": top_n,
                "market_filter": market,
                "strategy_version": strategy_version,
                "gate_reason": validation_meta.get("gate_reason", ""),
                "validation_gate": validation_meta.get("validation_gate", {}),
                "strategy": "实时行情不可用，显示最近保存的明日优先推荐",
                "fallback": "saved_snapshot",
                "risk_blacklist": risk_blacklist_summary(load_risk_blacklist_fn()),
                "hard_filter_report": {"raw_count": 0, "passed_count": len(saved_rows), "rejected_count": 0, "reasons": []},
            },
        )

    fallback_meta = {
        "generated_at": "",
        "candidate_count": len(saved_rows),
        "screened_count": len(saved_rows),
        "display_count": len(display_rows),
        "display_limit": display_limit,
        "display_cap": getattr(config, "TOMORROW_RECOMMENDATION_DISPLAY_LIMIT", display_limit),
        "min_score": 0.0,
        "gate_reason": validation_meta.get("gate_reason", ""),
        "primary_watch_count": tier_counts["primary_watch_count"],
        "backup_watch_count": tier_counts["backup_watch_count"],
        "top_n": top_n,
        "market_filter": market,
        "analysis_window": analysis_window_fn(),
        "strategy_version": strategy_version,
        "strategy_label": "明日优先",
        "prediction_type": "rank_score",
        "score_note": "综合分是量价/趋势/风险排序分，不等于上涨概率，也不代表保证收益。",
        "holding_discipline": "尾盘确认后入场，主验证周期为次日收盘；高开超过阈值不追",
        "profit_window": "次日",
        "recommendation_class": "next_day_priority",
        "recommendation_class_label": "明日优先",
        "strategy": "实时行情不可用，显示最近保存的明日优先推荐",
        "fallback": "saved_snapshot",
        "validation_gate": validation_meta.get("validation_gate", {}),
        "policy": {
            "main_max_gain": config.MAX_BUYABLE_GAIN_MAIN,
            "growth_max_gain": config.MAX_BUYABLE_GAIN_GROWTH,
            "min_turnover": config.MIN_TURNOVER,
            "avoid_limit_up": True,
        },
    }
    return response_payload(
        provider_health_fn,
        research_disclaimer_fn,
        ok=True,
        include_disclaimer=True,
        data=display_rows,
        meta=fallback_meta,
    )


def _saved_tomorrow_strategy_version(rows: List[Dict[str, object]]) -> str:
    for row in rows or []:
        version = str(row.get("strategy_version") or "").strip()
        if version:
            return version
    return str(getattr(config, "TOMORROW_STRATEGY_VERSION", "tomorrow_picks_v8_next_day"))


def _saved_tomorrow_tier_counts(rows: List[Dict[str, object]]) -> Dict[str, int]:
    rows = list(rows or [])
    has_tiers = any(str(row.get("tier") or "").strip() for row in rows)
    if has_tiers:
        primary = sum(1 for row in rows if row.get("tier") == "primary_watch")
    else:
        primary = min(int(getattr(config, "TOMORROW_PRIMARY_WATCH_N", 10)), len(rows))
    return {
        "primary_watch_count": primary,
        "backup_watch_count": max(0, len(rows) - primary),
    }
