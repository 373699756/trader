from typing import Dict, Iterable, List

from .event_risk import attach_event_risk, load_event_risk
from .fundamentals import attach_fundamental_factors, load_fundamentals
from .scoring import (
    build_market_regime,
    prepare_candidates,
    score_tomorrow_candidates,
)


SNAPSHOT_STRATEGIES = ("tomorrow_picks",)


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
    if strategy == "tomorrow_picks":
        rows, meta = score_tomorrow_candidates(candidates, top_n=50, market_filter=market, market_regime=market_regime)
        return rows, meta, meta.get("strategy_version", "tomorrow_picks_v3")
    raise KeyError("unsupported snapshot strategy: {}".format(strategy))
