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
    if not detailed:
        return response_payload(
            provider_health_fn,
            research_disclaimer_fn,
            ok=True,
            include_disclaimer=True,
            data=saved_rows[:top_n],
            meta={
                "generated_at": "",
                "candidate_count": len(saved_rows),
                "top_n": top_n,
                "market_filter": market,
                "strategy": "实时行情不可用，显示最近保存的明天推荐",
                "fallback": "saved_snapshot",
                "risk_blacklist": risk_blacklist_summary(load_risk_blacklist_fn()),
                "hard_filter_report": {"raw_count": 0, "passed_count": len(saved_rows), "rejected_count": 0, "reasons": []},
            },
        )

    fallback_meta = {
        "generated_at": "",
        "candidate_count": len(saved_rows),
        "screened_count": len(saved_rows),
        "display_count": min(len(saved_rows), top_n),
        "display_limit": top_n,
        "min_score": 0.0,
        "gate_reason": "实时行情不可用，显示最近保存快照；不代表今日实时盘面。",
        "primary_watch_count": min(int(getattr(config, "TOMORROW_PRIMARY_WATCH_N", 10)), len(saved_rows), top_n),
        "top_n": top_n,
        "market_filter": market,
        "analysis_window": analysis_window_fn(),
        "strategy_version": "tomorrow_picks_v5",
        "strategy_label": "明天推荐",
        "prediction_type": "rank_score",
        "score_note": "综合分是量价/趋势/风险排序分，不等于上涨概率，也不代表保证收益。",
        "strategy": "实时行情不可用，显示最近保存的明天推荐",
        "fallback": "saved_snapshot",
        "policy": {
            "main_max_gain": config.MAX_BUYABLE_GAIN_MAIN,
            "growth_max_gain": config.MAX_BUYABLE_GAIN_GROWTH,
            "min_turnover": config.MIN_TURNOVER,
            "avoid_limit_up": True,
        },
    }
    try:
        apply_tomorrow_validation_gate(
            saved_rows,
            fallback_meta,
            cached_metrics_fn("tomorrow_picks", 20),
        )
    except Exception:
        pass
    return response_payload(
        provider_health_fn,
        research_disclaimer_fn,
        ok=True,
        include_disclaimer=True,
        data=saved_rows[:top_n],
        meta=fallback_meta,
    )
