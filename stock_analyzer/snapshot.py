from datetime import datetime
from typing import Dict, Iterable, List

from . import config
from .app_runtime_support import apply_deepseek_rerank, finalize_deepseek_meta
from .daily_data import load_history_frames
from .event_risk import attach_event_risk, load_event_risk
from .execution_policy import build_execution_policy
from .factors import build_alphalite_factors, merge_alphalite
from .fundamentals import attach_fundamental_factors, load_fundamentals
from .normalization import coerce_number, normalize_code
from .point_in_time import (
    build_candidate_snapshot_rows,
    filter_point_in_time_events,
    filter_point_in_time_fundamentals,
)
from .production_baseline import attach_generation_provenance
from .scoring_core.candidate_filters import prepare_candidates
from .scoring_core.market_regime import build_market_regime
from .strategies import score_swing_2_5d_picks, score_today_picks, score_tomorrow_picks, storage_strategy_name


SNAPSHOT_STRATEGIES = tuple(config.SNAPSHOT_STRATEGIES)


def run_snapshot(provider, validation_store, strategy: str, market: str = "all") -> Dict[str, object]:
    strategy = storage_strategy_name(strategy)
    if strategy not in SNAPSHOT_STRATEGIES:
        return {"ok": False, "strategy": strategy, "error": "unknown_strategy"}
    quotes = provider.get_realtime_quotes()
    freshness_error = _quote_freshness_error(provider, quotes)
    if freshness_error:
        return {"ok": False, "strategy": strategy, "error": freshness_error, "saved": {"saved": 0, "replaced": 0}}
    snapshot_cutoff = datetime.now().isoformat(timespec="seconds")
    event_payload = filter_point_in_time_events(load_event_risk(provider), snapshot_cutoff)
    candidates = attach_event_risk(prepare_candidates(quotes), event_payload)
    codes = candidates["code"].tolist() if candidates is not None and "code" in candidates.columns else []
    fundamental_payload = filter_point_in_time_fundamentals(
        load_fundamentals(provider, codes=codes),
        snapshot_cutoff,
    )
    candidates = attach_fundamental_factors(candidates, fundamental_payload)
    candidates = _attach_snapshot_history_factors(provider, candidates)
    market_regime = build_market_regime(candidates, breadth_source=quotes)
    rows, meta, version = _score_snapshot_strategy(provider, candidates, quotes, strategy, market, market_regime)
    scored_candidate_rows = meta.pop("_candidate_pool_rows", None)
    rows, deepseek_meta = _apply_snapshot_deepseek_rerank(rows, strategy, market)
    finalize_deepseek_meta(meta, rows, deepseek_meta)
    if _after_close_anchor_time(meta["generated_at"]):
        rows, close_anchor = _apply_close_anchor_prices(provider, rows, meta["generated_at"], quotes)
        meta["close_anchor"] = {
            "enabled": True,
            **close_anchor,
            "time": getattr(config, "VALIDATION_CLOSE_ANCHOR_TIME", "15:00"),
        }
        if close_anchor["count"] < close_anchor["total"]:
            return {
                "ok": False,
                "strategy": strategy,
                "error": "15:00后收盘锚点不完整，拒绝保存为回溯锚点。",
                "saved": {"saved": 0, "replaced": 0},
                "meta": meta,
            }
    provider_health = _provider_health(provider)
    provenance = attach_generation_provenance(meta, strategy, rows, candidates)
    candidate_rows = build_candidate_snapshot_rows(
        quotes,
        candidates,
        rows,
        meta["generated_at"],
        scored_rows=scored_candidate_rows,
        event_payload=event_payload,
        fundamental_payload=fundamental_payload,
        provider_health=provider_health,
    )
    execution_policy = build_execution_policy(strategy, market)
    data_source_timestamp = str(
        (quotes.attrs or {}).get("quote_timestamp")
        or provider_health.get("last_quote_refresh")
        or ""
    )
    meta["point_in_time"] = {
        "candidate_count": len(candidate_rows),
        "eligible_count": sum(1 for row in candidate_rows if row.get("eligible")),
        "selected_count": sum(1 for row in candidate_rows if row.get("selected")),
        "valid_count": sum(1 for row in candidate_rows if row.get("point_in_time_valid")),
        "market_data_cutoff": meta["generated_at"],
        "data_source_timestamp": data_source_timestamp,
        "fundamentals": fundamental_payload.get("point_in_time") or {},
        "events": event_payload.get("point_in_time") or {},
    }
    meta["execution_policy_version"] = execution_policy["policy_version"]
    saved = validation_store.save_signals(
        strategy,
        version,
        meta["generated_at"],
        rows,
        deepseek_shadow_rows=(
            []
            if str(deepseek_meta.get("mode") or "") == "shadow_only"
            else deepseek_meta.get("filtered_rows") or []
        ),
        candidate_rows=candidate_rows,
        batch_metadata={
            "data_source_timestamp": data_source_timestamp,
            "market_data_cutoff": meta["generated_at"],
            "generation": provenance,
        },
        execution_policy=execution_policy,
    )
    return {"ok": True, "strategy": strategy, "saved": saved, "meta": meta}


def run_snapshots(provider, validation_store, strategies: Iterable[str], market: str = "all") -> List[Dict[str, object]]:
    return [run_snapshot(provider, validation_store, strategy, market=market) for strategy in strategies]


def _score_snapshot_strategy(provider, candidates, quotes, strategy: str, market: str, market_regime: Dict[str, object]):
    if strategy == "short_term":
        rows_by_horizon, meta = score_today_picks(
            candidates,
            hot_ranks={},
            industry_strength={},
            sentiment_lookup={},
            top_n=getattr(config, "RECOMMENDATION_DISPLAY_LIMIT", 18),
            market_filter=market,
            market_regime=market_regime,
            capture_candidate_pool=True,
        )
        rows = rows_by_horizon.get("short_term", [])
        return rows, meta, config.SHORT_TERM_STRATEGY_VERSION
    scorers = {
        "tomorrow_picks": (
            score_tomorrow_picks,
            getattr(config, "TOMORROW_SNAPSHOT_TOP_N", config.TOMORROW_TOP_N),
            {"display_cap": 0, "capture_candidate_pool": True},
        ),
        "swing_picks": (
            score_swing_2_5d_picks,
            getattr(config, "RECOMMENDATION_DISPLAY_LIMIT", 18),
            {"capture_candidate_pool": True},
        ),
    }
    scorer, top_n, extra_kwargs = scorers[strategy]
    rows, meta = scorer(candidates, top_n=top_n, market_filter=market, market_regime=market_regime, **extra_kwargs)
    return rows, meta, meta.get("strategy_version", strategy)


def _apply_snapshot_deepseek_rerank(rows: List[Dict[str, object]], strategy: str, market: str):
    return apply_deepseek_rerank(strategy, rows, market)


def _deepseek_rerank_disabled_strategies() -> set:
    raw = str(getattr(config, "DEEPSEEK_RERANK_DISABLED_STRATEGIES", "") or "").strip()
    if not raw:
        return set()
    return {item.strip() for item in raw.replace("，", ",").split(",") if item.strip()}


def _attach_snapshot_history_factors(provider, candidates):
    if candidates is None or candidates.empty or not getattr(config, "ENABLE_HISTORY_FACTORS", True):
        return candidates
    if "code" not in candidates.columns:
        return candidates
    target_codes = candidates.sort_values(["pct_chg", "turnover"], ascending=False).head(
        max(1, int(getattr(config, "HISTORY_FACTOR_LIMIT", 40)))
    )["code"].tolist()
    history_by_code = {}
    try:
        history_by_code.update(
            load_history_frames(getattr(config, "MARKET_DATA_DB_PATH", ""), target_codes, days=90)
        )
    except Exception:
        history_by_code = {}
    for code_value in target_codes:
        code = normalize_code(code_value)
        if not code or code in history_by_code:
            continue
        history = None
        try:
            if hasattr(provider, "get_cached_history"):
                history = provider.get_cached_history(code, days=90)
            elif hasattr(provider, "get_history"):
                history = provider.get_history(code, days=90)
        except Exception:
            history = None
        if history is not None and not history.empty:
            history_by_code[code] = history
    if not history_by_code:
        return candidates
    factors = build_alphalite_factors(history_by_code)
    if factors is not None and not factors.empty:
        cutoffs = {}
        for code, history in history_by_code.items():
            if history is None or history.empty or "trade_date" not in history.columns:
                continue
            cutoffs[normalize_code(code)] = str(history["trade_date"].iloc[-1])
        factors["history_data_cutoff"] = factors["code"].map(cutoffs).fillna("")
    return merge_alphalite(candidates, factors)


def _apply_close_anchor_prices(provider, rows: List[Dict[str, object]], signal_time: str, quotes):
    if not rows or not _after_close_anchor_time(signal_time):
        return rows, {"count": 0, "total": len(rows or []), "missing": []}
    signal_date = str(signal_time or "")[:10]
    quote_anchors = _quote_anchor_lookup(quotes)
    allow_quote_fallback = _allow_quote_close_fallback(provider, signal_time)
    anchored: List[Dict[str, object]] = []
    missing: List[str] = []
    for row in rows:
        item = dict(row)
        code = normalize_code(item.get("code"))
        close_price, pct_chg = _history_anchor_for_date(provider, code, signal_date)
        source = "history_close" if close_price > 0 else ""
        if close_price <= 0 and allow_quote_fallback:
            quote_anchor = quote_anchors.get(code, {})
            close_price = coerce_number(quote_anchor.get("price"))
            pct_chg = coerce_number(quote_anchor.get("pct_chg"))
            source = "quote_close" if close_price > 0 else ""
        if close_price > 0:
            item["price"] = round(close_price, 4)
            item["pct_chg"] = round(pct_chg, 4)
            item["anchor_price_source"] = source
            item["anchor_price_time"] = signal_time
        else:
            missing.append(code or str(item.get("code") or ""))
        anchored.append(item)
    return anchored, {"count": len(anchored) - len(missing), "total": len(anchored), "missing": missing[:10]}


def _after_close_anchor_time(signal_time: str) -> bool:
    raw_time = str(signal_time or "")
    if "T" not in raw_time:
        return False
    try:
        stamp = datetime.fromisoformat(raw_time)
    except ValueError:
        return False
    hour, minute = _time_parts(getattr(config, "VALIDATION_CLOSE_ANCHOR_TIME", "15:00"), (15, 0))
    close_at = stamp.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return stamp >= close_at


def _time_parts(value: str, fallback: tuple) -> tuple:
    raw = str(value or "").strip()
    try:
        hour_text, minute_text = raw.split(":", 1)
        return min(23, max(0, int(hour_text))), min(59, max(0, int(minute_text)))
    except Exception:
        return fallback


def _quote_anchor_lookup(quotes) -> Dict[str, Dict[str, float]]:
    if quotes is None or getattr(quotes, "empty", True) or "code" not in quotes.columns:
        return {}
    lookup: Dict[str, Dict[str, float]] = {}
    for _, row in quotes.iterrows():
        code = normalize_code(row.get("code"))
        price = coerce_number(row.get("price"))
        if code and price > 0:
            lookup[code] = {"price": price, "pct_chg": coerce_number(row.get("pct_chg"))}
    return lookup


def _history_anchor_for_date(provider, code: str, signal_date: str):
    if not code or not signal_date:
        return 0.0, 0.0
    target = signal_date.replace("-", "")
    try:
        history = provider.get_history(code, days=10)
    except Exception:
        return 0.0, 0.0
    if history is None or history.empty or "trade_date" not in history.columns:
        return 0.0, 0.0
    history = history.copy()
    history["_date"] = history["trade_date"].astype(str).str.replace("-", "", regex=False)
    history = history.sort_values("_date").reset_index(drop=True)
    today = history[history["_date"] == target]
    if today.empty:
        return 0.0, 0.0
    today_index = int(today.index[-1])
    row = history.iloc[today_index]
    close_price = coerce_number(row.get("price") if "price" in history.columns else row.get("close"))
    pct_chg = coerce_number(row.get("pct_chg")) if "pct_chg" in history.columns else 0.0
    if not pct_chg and today_index > 0:
        prev = history.iloc[today_index - 1]
        prev_close = coerce_number(prev.get("price") if "price" in history.columns else prev.get("close"))
        if close_price > 0 and prev_close > 0:
            pct_chg = round((close_price / prev_close - 1) * 100, 4)
    return close_price, pct_chg


def _allow_quote_close_fallback(provider, signal_time: str) -> bool:
    health_fn = getattr(provider, "health", None)
    if not callable(health_fn):
        return True
    health = health_fn() or {}
    source = str(health.get("quotes_source") or "")
    if "快照" not in source:
        return True
    refreshed = health.get("last_quote_refresh")
    try:
        refreshed_at = datetime.fromisoformat(str(refreshed))
        stamp = datetime.fromisoformat(str(signal_time))
    except Exception:
        return False
    hour, minute = _time_parts(getattr(config, "VALIDATION_CLOSE_ANCHOR_TIME", "15:00"), (15, 0))
    close_at = stamp.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return refreshed_at >= close_at


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
    if "快照" in source and not getattr(config, "VALIDATION_ALLOW_LOCAL_QUOTE_SNAPSHOT", False):
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


def _provider_health(provider) -> Dict[str, object]:
    health_fn = getattr(provider, "health", None)
    if not callable(health_fn):
        return {}
    try:
        result = health_fn() or {}
    except Exception:
        return {}
    return result if isinstance(result, dict) else {}
