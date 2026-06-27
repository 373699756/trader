from typing import Dict, Iterable, List

from . import config
from .event_risk import attach_event_risk, load_event_risk
from .factors import build_alphalite_factors, merge_alphalite
from .fundamentals import attach_fundamental_factors, load_fundamentals
from .normalization import normalize_code
from .scoring import (
    build_market_regime,
    prepare_candidates,
    score_breakout_candidates,
    score_chokepoint_candidates,
    score_dual_horizon_candidates,
    score_position_candidates,
    score_reversal_candidates,
    score_smallcap_value_candidates,
    score_swing_candidates,
    score_tech_potential_candidates,
    score_tomorrow_candidates,
)


SNAPSHOT_STRATEGIES = (
    "short_term",
    "long_term",
    "tomorrow_picks",
    "swing_picks",
    "position_picks",
    "tech_potential",
    "chokepoint_picks",
    "reversal_picks",
    "smallcap_value_picks",
    "breakout_picks",
)


def run_snapshot(provider, validation_store, strategy: str, market: str = "all") -> Dict[str, object]:
    if strategy not in SNAPSHOT_STRATEGIES:
        return {"ok": False, "strategy": strategy, "error": "unknown_strategy"}
    quotes = provider.get_realtime_quotes()
    candidates = attach_event_risk(prepare_candidates(quotes), load_event_risk(provider))
    codes = candidates["code"].tolist() if candidates is not None and "code" in candidates.columns else []
    candidates = attach_fundamental_factors(candidates, load_fundamentals(provider, codes=codes))
    market_regime = build_market_regime(candidates, breadth_source=quotes)
    rows, meta, version = _score_snapshot_strategy(provider, candidates, quotes, strategy, market, market_regime)
    saved = validation_store.save_signals(strategy, version, meta["generated_at"], rows)
    return {"ok": True, "strategy": strategy, "saved": saved, "meta": meta}


def run_snapshots(provider, validation_store, strategies: Iterable[str], market: str = "all") -> List[Dict[str, object]]:
    return [run_snapshot(provider, validation_store, strategy, market=market) for strategy in strategies]


def _score_snapshot_strategy(provider, candidates, quotes, strategy: str, market: str, market_regime: Dict[str, object]):
    if strategy in ("short_term", "long_term"):
        rows_by_horizon, meta = score_dual_horizon_candidates(candidates, top_n=50, market_filter=market)
        rows = rows_by_horizon["short_term" if strategy == "short_term" else "long_term"]
        return rows, meta, "dual_horizon_v2"
    if strategy == "tomorrow_picks":
        rows, meta = score_tomorrow_candidates(candidates, top_n=50, market_filter=market, market_regime=market_regime)
        return rows, meta, meta.get("strategy_version", "tomorrow_picks_v2")
    if strategy == "tech_potential":
        rows, meta = score_tech_potential_candidates(candidates, top_n=50, market_filter=market, market_regime=market_regime)
        return rows, meta, "tech_potential_v1"
    if strategy == "chokepoint_picks":
        rows, meta = score_chokepoint_candidates(candidates, top_n=30, market_filter=market, market_regime=market_regime)
        return rows, meta, meta.get("strategy_version", "chokepoint_v1")

    candidates = _attach_history_factors(provider, candidates)
    scorer = {
        "swing_picks": score_swing_candidates,
        "position_picks": score_position_candidates,
        "reversal_picks": score_reversal_candidates,
        "smallcap_value_picks": score_smallcap_value_candidates,
        "breakout_picks": score_breakout_candidates,
    }[strategy]
    rows, meta = scorer(candidates, top_n=30, market_filter=market, market_regime=market_regime)
    version = {
        "swing_picks": "swing_5_10d_v1",
        "position_picks": "position_1_3m_v1",
        "reversal_picks": "reversal_v1",
        "smallcap_value_picks": "smallcap_value_v1",
        "breakout_picks": "breakout_v1",
    }.get(strategy, strategy.replace("_picks", "_v1"))
    return rows, meta, meta.get("strategy_version", version)


def _attach_history_factors(provider, candidates):
    if not config.ENABLE_HISTORY_FACTORS or candidates is None or candidates.empty:
        return candidates
    history_by_code = {}
    for code in candidates.head(config.HISTORY_FACTOR_LIMIT)["code"].tolist():
        normalized = normalize_code(code)
        try:
            history = provider.get_history(normalized, days=90)
        except Exception:
            continue
        if history is not None and not history.empty:
            history_by_code[normalized] = history
    if not history_by_code:
        return candidates
    return merge_alphalite(candidates, build_alphalite_factors(history_by_code))
