import math
from typing import Dict, List, Tuple

import pandas as pd

from . import config
from .app_runtime_support import (
    apply_deepseek_rerank,
    apply_deepseek_rerank_batch,
    finalize_deepseek_meta,
    risk_blacklist_summary,
    skipped_deepseek_meta,
)
from .app_support import apply_tomorrow_validation_gate, attach_validation_summary
from .scoring import limit_theme_concentration
from .strategies import score_swing_2_5d_picks, score_today_picks, score_tomorrow_picks


def scored_strategy_rows(
    strategy_name: str,
    candidates: pd.DataFrame,
    top_n: int,
    market: str,
    market_regime: Dict[str, object],
    apply_deepseek: bool = True,
) -> Tuple[List[Dict[str, object]], Dict[str, object], Dict[str, object]]:
    if strategy_name == "tomorrow_picks":
        rows, meta = score_tomorrow_picks(
            candidates,
            top_n=top_n,
            market_filter=market,
            market_regime=market_regime,
        )
    elif strategy_name == "swing_picks":
        rows, meta = score_swing_2_5d_picks(
            candidates,
            top_n=top_n,
            market_filter=market,
            market_regime=market_regime,
        )
    else:
        raise ValueError(f"Unsupported strategy for scored rows: {strategy_name}")
    if apply_deepseek:
        rows, deepseek_meta = apply_deepseek_rerank(strategy_name, rows, market)
    else:
        deepseek_meta = skipped_deepseek_meta(strategy_name)
    finalize_deepseek_meta(meta, rows, deepseek_meta)
    return rows, meta, deepseek_meta


def prediction_strategy_rows(
    candidates: pd.DataFrame,
    top_n: int,
    market_regime: Dict[str, object],
    hot_ranks: Dict[str, int],
    industry_strength: Dict[str, float],
    sentiment_lookup: Dict[str, Dict[str, object]],
    short_term_rows_override: List[Dict[str, object]] = None,
    short_term_meta_override: Dict[str, object] = None,
) -> Tuple[Dict[str, List[Dict[str, object]]], Dict[str, Dict[str, object]]]:
    today_rows, today_meta = score_today_picks(
        candidates,
        hot_ranks=hot_ranks,
        industry_strength=industry_strength,
        sentiment_lookup=sentiment_lookup,
        top_n=top_n,
        market_regime=market_regime,
    )
    short_rows = today_rows.get("short_term", [])
    short_rows, short_deepseek_meta = apply_deepseek_rerank("short_term", short_rows, "all")
    if short_term_rows_override is not None:
        short_rows = list(short_term_rows_override)

    tomorrow_rows, tomorrow_meta, tomorrow_deepseek_meta = scored_strategy_rows(
        "tomorrow_picks",
        candidates,
        top_n=top_n,
        market="all",
        market_regime=market_regime,
    )
    swing_rows, swing_meta, swing_deepseek_meta = scored_strategy_rows(
        "swing_picks",
        candidates,
        top_n=top_n,
        market="all",
        market_regime=market_regime,
    )
    return {
        "short_term": short_rows,
        "tomorrow_picks": tomorrow_rows,
        "swing_picks": swing_rows,
    }, {
        "short_term": {**today_meta, "deepseek": short_deepseek_meta, **(short_term_meta_override or {})},
        "tomorrow_picks": {**tomorrow_meta, "deepseek": tomorrow_deepseek_meta},
        "swing_picks": {**swing_meta, "deepseek": swing_deepseek_meta},
    }


def build_recommendation_horizons(
    candidates: pd.DataFrame,
    top_n: int,
    market: str,
    market_regime: Dict[str, object],
    hot_ranks: Dict[str, int],
    industry_strength: Dict[str, float],
    sentiment_lookup: Dict[str, Dict[str, object]],
    cached_metrics_fn,
    apply_deepseek: bool = True,
) -> Tuple[Dict[str, List[Dict[str, object]]], Dict[str, object], Dict[str, object]]:
    recommendations_by_horizon, short_meta = score_today_picks(
        candidates,
        hot_ranks=hot_ranks,
        industry_strength=industry_strength,
        sentiment_lookup=sentiment_lookup,
        top_n=top_n,
        market_filter=market,
        market_regime=market_regime,
    )

    tomorrow_rows, tomorrow_meta = score_tomorrow_picks(
        candidates,
        top_n=top_n,
        market_filter=market,
        market_regime=market_regime,
    )

    swing_rows, swing_meta = score_swing_2_5d_picks(
        candidates,
        top_n=top_n,
        market_filter=market,
        market_regime=market_regime,
    )
    if apply_deepseek:
        recommendations_by_horizon, deepseek_meta_by_strategy = apply_deepseek_rerank_batch(
            {
                "short_term": recommendations_by_horizon["short_term"],
                "tomorrow_picks": tomorrow_rows,
                "swing_picks": swing_rows,
            },
            market,
        )
    else:
        recommendations_by_horizon["tomorrow_picks"] = tomorrow_rows
        recommendations_by_horizon["swing_picks"] = swing_rows
        deepseek_meta_by_strategy = {
            "short_term": skipped_deepseek_meta("short_term"),
            "tomorrow_picks": skipped_deepseek_meta("tomorrow_picks"),
            "swing_picks": skipped_deepseek_meta("swing_picks"),
        }
    short_deepseek_meta = deepseek_meta_by_strategy.get("short_term", skipped_deepseek_meta("short_term"))
    tomorrow_deepseek_meta = deepseek_meta_by_strategy.get("tomorrow_picks", skipped_deepseek_meta("tomorrow_picks"))
    swing_deepseek_meta = deepseek_meta_by_strategy.get("swing_picks", skipped_deepseek_meta("swing_picks"))
    tomorrow_rows = recommendations_by_horizon.get("tomorrow_picks", tomorrow_rows)
    swing_rows = recommendations_by_horizon.get("swing_picks", swing_rows)
    finalize_deepseek_meta(short_meta, recommendations_by_horizon["short_term"], short_deepseek_meta)
    finalize_deepseek_meta(tomorrow_meta, tomorrow_rows, tomorrow_deepseek_meta)
    try:
        apply_tomorrow_validation_gate(
            tomorrow_rows,
            tomorrow_meta,
            cached_metrics_fn("tomorrow_picks", 20),
        )
    except Exception:
        pass
    finalize_deepseek_meta(swing_meta, swing_rows, swing_deepseek_meta)

    recommendations_by_horizon["tomorrow_picks"] = tomorrow_rows
    recommendations_by_horizon["swing_picks"] = swing_rows
    market_gate = _review_market_gate(candidates, market_regime, apply_deepseek=apply_deepseek)
    if market_gate.get("enabled") and market_gate.get("status") in {"ok", "fallback"}:
        recommendations_by_horizon, gate_counts = _apply_market_gate(recommendations_by_horizon, market_gate)
        market_gate["counts"] = gate_counts
        short_meta["deepseek_market_gate"] = market_gate
    return recommendations_by_horizon, short_meta, {
        "short_term": short_deepseek_meta,
        "tomorrow_picks": tomorrow_deepseek_meta,
        "swing_picks": swing_deepseek_meta,
    }


def _review_market_gate(candidates: pd.DataFrame, market_regime: Dict[str, object], apply_deepseek: bool) -> Dict[str, object]:
    if not apply_deepseek or not getattr(config, "ENABLE_DEEPSEEK_MARKET_GATE", False):
        return {"enabled": False, "status": "disabled"}
    context = _market_gate_context(candidates, market_regime)
    try:
        from .deepseek_client import review_market_regime

        result = review_market_regime(context)
        result["context"] = context
        return result
    except Exception as exc:
        return {"enabled": True, "status": "fallback", "error": str(exc), "context": context}


def _market_gate_context(candidates: pd.DataFrame, market_regime: Dict[str, object]) -> Dict[str, object]:
    if isinstance(market_regime, dict) and int(market_regime.get("breadth_sample_count") or 0) > 0:
        total = int(market_regime.get("breadth_sample_count") or 0)
        up_count = int(market_regime.get("up_count") or 0)
        down_count = int(market_regime.get("down_count") or 0)
        return {
            "market_regime": market_regime or {},
            "sample_count": total,
            "breadth_source": "full_market_snapshot",
            "up_count": up_count,
            "down_count": down_count,
            "up_ratio_pct": round(up_count / max(total, 1) * 100.0, 2),
            "down_ratio_pct": round(down_count / max(total, 1) * 100.0, 2),
            "limit_up_count": int(market_regime.get("limit_up_count") or 0),
            "limit_down_count": int(market_regime.get("limit_down_count") or 0),
            "avg_pct_chg": market_regime.get("avg_pct_chg", market_regime.get("median_pct_chg", 0.0)),
            "median_pct_chg": market_regime.get("median_pct_chg", 0.0),
            "turnover_total": None,
        }
    if candidates is None or candidates.empty:
        return {"market_regime": market_regime or {}, "sample_count": 0}
    pct_source = candidates["pct_chg"] if "pct_chg" in candidates else pd.Series([0.0] * len(candidates))
    turnover_source = candidates["turnover"] if "turnover" in candidates else pd.Series([0.0] * len(candidates))
    pct = pd.to_numeric(pct_source, errors="coerce").fillna(0.0)
    turnover = pd.to_numeric(turnover_source, errors="coerce").fillna(0.0)
    total = int(len(candidates))
    up_count = int((pct > 0).sum())
    down_count = int((pct < 0).sum())
    limit_up_count = int((pct >= 9.5).sum())
    limit_down_count = int((pct <= -9.5).sum())
    return {
        "market_regime": market_regime or {},
        "sample_count": total,
        "breadth_source": "candidate_pool",
        "up_count": up_count,
        "down_count": down_count,
        "up_ratio_pct": round(up_count / max(total, 1) * 100.0, 2),
        "down_ratio_pct": round(down_count / max(total, 1) * 100.0, 2),
        "limit_up_count": limit_up_count,
        "limit_down_count": limit_down_count,
        "avg_pct_chg": round(float(pct.mean()), 4),
        "median_pct_chg": round(float(pct.median()), 4),
        "turnover_total": round(float(turnover.sum()), 2),
    }


def _apply_market_gate(
    recommendations_by_horizon: Dict[str, List[Dict[str, object]]],
    market_gate: Dict[str, object],
) -> Tuple[Dict[str, List[Dict[str, object]]], Dict[str, Dict[str, int]]]:
    factor = max(0.0, min(1.0, float(market_gate.get("size_factor") or 1.0)))
    regime = str(market_gate.get("regime") or "balanced")
    if factor >= 0.999 and regime != "risk_off":
        return recommendations_by_horizon, {
            strategy: {"before": len(rows or []), "after": len(rows or [])}
            for strategy, rows in recommendations_by_horizon.items()
        }
    gated: Dict[str, List[Dict[str, object]]] = {}
    counts: Dict[str, Dict[str, int]] = {}
    risk_off_min_score = coerce_market_gate_tomorrow_min_score() if regime == "risk_off" else None
    for strategy, rows in recommendations_by_horizon.items():
        source_rows = list(rows or [])
        filtered_rows = source_rows
        if strategy == "tomorrow_picks" and risk_off_min_score is not None:
            threshold_rows = [row for row in source_rows if float(row.get("score") or 0.0) >= risk_off_min_score]
            if threshold_rows:
                filtered_rows = threshold_rows
        keep = max(1, int(math.ceil(len(filtered_rows) * factor))) if filtered_rows else 0
        gated[strategy] = filtered_rows[:keep]
        counts[strategy] = {"before": len(source_rows), "after": len(gated[strategy])}
    return gated, counts


def coerce_market_gate_tomorrow_min_score() -> float:
    base = float(getattr(config, "TOMORROW_PRIMARY_MIN_SCORE", 68.0))
    bonus = float(getattr(config, "DEEPSEEK_MARKET_GATE_RISK_OFF_SCORE_BONUS", 5.0))
    return base + bonus


def finalize_recommendation_payload_meta(
    short_rows: List[Dict[str, object]],
    meta: Dict[str, object],
    blacklist_payload: Dict[str, object],
    hard_filter_report: Dict[str, object],
    market_regime: Dict[str, object],
    deepseek_meta_by_strategy: Dict[str, object],
    top_n: int,
    stability_update_fn,
    validation_store,
    cached_metrics_fn,
) -> Tuple[List[Dict[str, object]], Dict[str, object]]:
    short_stability = stability_update_fn("short_term", short_rows)
    theme_cap = int(getattr(config, "RECOMMENDATION_MAX_DISPLAY_PER_THEME", 3))
    short_display_rows, short_theme_limited = limit_theme_concentration(short_stability["rows"], top_n, theme_cap)
    attach_validation_summary(short_display_rows, validation_store, "short_term", metrics_fn=cached_metrics_fn)
    meta["top_n"] = top_n
    meta["risk_blacklist"] = risk_blacklist_summary(blacklist_payload)
    meta["hard_filter_report"] = hard_filter_report
    meta["stability"] = {
        "short_term": {
            "new_entries": short_stability["new_entries"],
            "dropped": short_stability["dropped"],
            "retained": short_stability["retained"],
            "last_updated": short_stability["last_updated"],
        },
    }
    meta["deepseek"] = {
        strategy: _public_deepseek_meta(item)
        for strategy, item in (deepseek_meta_by_strategy or {}).items()
    }
    meta["market_regime"] = market_regime
    meta["display_theme_cap"] = theme_cap
    meta["display_theme_limited"] = {
        "short_term": short_theme_limited,
    }
    return short_display_rows, meta


def _public_deepseek_meta(deepseek_meta: Dict[str, object]) -> Dict[str, object]:
    item = dict(deepseek_meta or {})
    item.pop("filtered_rows", None)
    return item
