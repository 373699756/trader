from typing import Dict, List, Tuple

import pandas as pd

from . import config
from .app_runtime_support import (
    apply_deepseek_rerank,
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
    if apply_deepseek:
        recommendations_by_horizon["short_term"], short_deepseek_meta = apply_deepseek_rerank(
            "short_term",
            recommendations_by_horizon["short_term"],
            market,
        )
    else:
        short_deepseek_meta = skipped_deepseek_meta("short_term")
    finalize_deepseek_meta(short_meta, recommendations_by_horizon["short_term"], short_deepseek_meta)

    tomorrow_rows, tomorrow_meta = score_tomorrow_picks(
        candidates,
        top_n=top_n,
        market_filter=market,
        market_regime=market_regime,
    )
    if apply_deepseek:
        tomorrow_rows, tomorrow_deepseek_meta = apply_deepseek_rerank(
            "tomorrow_picks",
            tomorrow_rows,
            market,
        )
    else:
        tomorrow_deepseek_meta = skipped_deepseek_meta("tomorrow_picks")
    finalize_deepseek_meta(tomorrow_meta, tomorrow_rows, tomorrow_deepseek_meta)
    try:
        apply_tomorrow_validation_gate(
            tomorrow_rows,
            tomorrow_meta,
            cached_metrics_fn("tomorrow_picks", 20),
        )
    except Exception:
        pass

    swing_rows, swing_meta = score_swing_2_5d_picks(
        candidates,
        top_n=top_n,
        market_filter=market,
        market_regime=market_regime,
    )
    if apply_deepseek:
        swing_rows, swing_deepseek_meta = apply_deepseek_rerank("swing_picks", swing_rows, market)
    else:
        swing_deepseek_meta = skipped_deepseek_meta("swing_picks")
    finalize_deepseek_meta(swing_meta, swing_rows, swing_deepseek_meta)

    recommendations_by_horizon["tomorrow_picks"] = tomorrow_rows
    recommendations_by_horizon["swing_picks"] = swing_rows
    return recommendations_by_horizon, short_meta, {
        "short_term": short_deepseek_meta,
        "tomorrow_picks": tomorrow_deepseek_meta,
        "swing_picks": swing_deepseek_meta,
    }


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
    meta["deepseek"] = deepseek_meta_by_strategy
    meta["market_regime"] = market_regime
    meta["display_theme_cap"] = theme_cap
    meta["display_theme_limited"] = {
        "short_term": short_theme_limited,
    }
    return short_display_rows, meta
