from typing import Dict, List

from . import config
from .app_support import (
    apply_strategy_validation_gate,
    apply_tomorrow_validation_gate,
    attach_validation_summary,
    demote_strategy_rows_to_backup,
    demote_tomorrow_rows_to_backup,
    validation_gate_window_days,
)
from .app_runtime_support import risk_blacklist_summary
from .production_baseline import attach_generation_provenance


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
            cached_metrics_fn("tomorrow_picks", validation_gate_window_days()),
        )
    except Exception as exc:
        reason = "验证指标读取失败，保存快照强制降级为备选：{}".format(exc)
        validation_meta["validation_gate"] = {
            "state": "unavailable",
            "blocked": True,
            "allows_backup": True,
            "reason": reason,
        }
        demote_tomorrow_rows_to_backup(saved_rows, validation_meta, reason)
    display_rows = saved_rows[:display_limit]
    tier_counts = _saved_tomorrow_tier_counts(display_rows)
    strategy_version = _saved_tomorrow_strategy_version(saved_rows)
    attach_generation_provenance(
        validation_meta,
        "tomorrow_picks",
        display_rows,
        saved_rows,
    )
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
                "generation": validation_meta["generation"],
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
        "holding_discipline": "T日14:30形成推荐，14:35前冻结；验证使用14:30后信号参考价并按T+1规则退出",
        "profit_window": "T日14:30后至T+1规则退出",
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
        "generation": validation_meta["generation"],
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
    return str(
        getattr(config, "TOMORROW_STRATEGY_VERSION", "tomorrow_picks_v12_post_1430_t1_exit")
    )


def saved_swing_fallback_payload(
    *,
    saved_rows: List[Dict[str, object]],
    top_n: int,
    market: str,
    validation_store,
    cached_metrics_fn,
    provider_health_fn,
    research_disclaimer_fn,
) -> Dict[str, object]:
    attach_validation_summary(saved_rows, validation_store, "swing_picks", metrics_fn=cached_metrics_fn)
    validation_meta = {
        "gate_reason": "实时行情不可用，显示最近保存快照；不代表今日实时盘面。",
    }
    try:
        apply_strategy_validation_gate(
            "swing_picks",
            saved_rows,
            validation_meta,
            cached_metrics_fn("swing_picks", validation_gate_window_days()),
        )
    except Exception as exc:
        reason = "验证指标读取失败，保存快照强制降级为备选：{}".format(exc)
        validation_meta["validation_gate"] = {
            "state": "unavailable",
            "blocked": True,
            "allows_backup": True,
            "reason": reason,
        }
        demote_strategy_rows_to_backup("swing_picks", saved_rows, validation_meta, reason)

    display_rows = saved_rows[: max(0, int(top_n or 0))]
    primary_count = sum(1 for row in display_rows if row.get("tier") == "primary_watch")
    strategy_version = next(
        (str(row.get("strategy_version")) for row in saved_rows if row.get("strategy_version")),
        str(getattr(config, "SWING_STRATEGY_VERSION", "swing_2_5d_v4_post_1430_entry")),
    )
    attach_generation_provenance(
        validation_meta,
        "swing_picks",
        display_rows,
        saved_rows,
    )
    return response_payload(
        provider_health_fn,
        research_disclaimer_fn,
        ok=True,
        include_disclaimer=True,
        data=display_rows,
        meta={
            "generated_at": "",
            "candidate_count": len(saved_rows),
            "screened_count": len(saved_rows),
            "display_count": len(display_rows),
            "display_limit": max(0, int(top_n or 0)),
            "top_n": top_n,
            "market_filter": market,
            "strategy_version": strategy_version,
            "strategy_label": "2-5日持有",
            "strategy": "后台刷新中，先显示最近保存的2-5日持有推荐",
            "fallback": "saved_snapshot",
            "primary_watch_count": primary_count,
            "backup_watch_count": max(0, len(display_rows) - primary_count),
            "gate_reason": validation_meta.get("gate_reason", ""),
            "validation_gate": validation_meta.get("validation_gate", {}),
            "generation": validation_meta["generation"],
        },
    )


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
