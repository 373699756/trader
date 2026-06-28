from datetime import datetime
from typing import Dict, Iterable, List

from .event_risk import attach_event_risk, load_event_risk
from .fundamentals import attach_fundamental_factors, load_fundamentals
from . import config
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
    freshness_error = _quote_freshness_error(provider, quotes)
    if freshness_error:
        return {"ok": False, "strategy": strategy, "error": freshness_error, "saved": {"saved": 0, "replaced": 0}}
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
        rows, meta = score_tomorrow_candidates(candidates, top_n=config.TOMORROW_TOP_N, market_filter=market, market_regime=market_regime)
        return rows, meta, meta.get("strategy_version", "tomorrow_picks_v4")
    raise KeyError("unsupported snapshot strategy: {}".format(strategy))


def _quote_freshness_error(provider, quotes) -> str:
    if quotes is None or quotes.empty:
        return "行情为空，拒绝保存明天预测快照。"
    min_rows = int(getattr(config, "QUOTE_SNAPSHOT_MIN_ROWS", 50))
    if len(quotes) < min_rows:
        return "行情行数不足 {} 条，拒绝保存明天预测快照。".format(min_rows)
    health_fn = getattr(provider, "health", None)
    if not callable(health_fn):
        return ""
    health = health_fn() or {}
    source = str(health.get("quotes_source") or "")
    if not source or source == "unavailable":
        return "行情来源不可用，拒绝保存明天预测快照。"
    if "快照" in source:
        return "当前行情来自本地快照，拒绝保存为今日真实预测。"
    refreshed = health.get("last_quote_refresh")
    if not refreshed:
        return "缺少行情刷新时间，拒绝保存明天预测快照。"
    try:
        refreshed_at = datetime.fromisoformat(str(refreshed))
    except ValueError:
        return "行情刷新时间格式异常，拒绝保存明天预测快照。"
    max_age = int(getattr(config, "VALIDATION_SNAPSHOT_MAX_QUOTE_AGE_SECONDS", 900))
    age = (datetime.now() - refreshed_at).total_seconds()
    if age > max_age:
        return "行情已超过 {} 秒未刷新，拒绝保存明天预测快照。".format(max_age)
    return ""
