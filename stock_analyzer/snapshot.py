from datetime import datetime
from typing import Dict, Iterable, List
from uuid import uuid4

import pandas as pd

from . import config
from .app_runtime_support import finalize_deepseek_meta
from .daily_data import load_history_frames
from .deepseek.runtime_features import attach_persisted_deepseek_features
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
from .validation_repository import SignalFreezeDeadlineExceeded
from .validation_policy import current_strategy_version


SNAPSHOT_STRATEGIES = tuple(config.SNAPSHOT_STRATEGIES)
SNAPSHOT_REQUIRED_FACTOR_FIELDS = ("volume_ratio", "turnover_rate", "amplitude")
SNAPSHOT_SAMPLE_TYPE = "real_forward"
SNAPSHOT_SAMPLE_SOURCE = "intraday_pit_14_30"


def run_snapshot(
    provider,
    validation_store,
    strategy: str,
    market: str = "all",
    _context: Dict[str, object] = None,
) -> Dict[str, object]:
    strategy = storage_strategy_name(strategy)
    if strategy not in SNAPSHOT_STRATEGIES:
        return {"ok": False, "strategy": strategy, "error": "unknown_strategy"}
    context = _context or _prepare_snapshot_context(provider)
    if context.get("error"):
        return _snapshot_error_rejection(
            strategy=strategy,
            provider=provider,
            validation_store=validation_store,
            snapshot_id=str(context.get("snapshot_id") or "snapshot_{}".format(uuid4().hex)),
            signal_time=str(context.get("snapshot_cutoff") or datetime.now().isoformat(timespec="seconds")),
            candidates=context.get("candidates") or pd.DataFrame(),
            quotes=context.get("quotes"),
            event_payload=context.get("event_payload") or {},
            fundamental_payload=context.get("fundamental_payload") or {},
            error=context["error"],
            market=market,
            market_regime=dict(context.get("market_regime") or {}),
            generation_status="context_rejection",
            reason="snapshot_context_rejected",
        )

    quotes = context["quotes"]
    snapshot_cutoff = str(context["snapshot_cutoff"])
    snapshot_id = str(context["snapshot_id"])
    event_payload = context["event_payload"]
    fundamental_payload = context["fundamental_payload"]
    candidates = context["candidates"].copy(deep=True)
    market_regime = dict(context["market_regime"])
    common_signal_time = str(context.get("signal_time") or snapshot_cutoff)
    context["signal_time"] = common_signal_time
    if _signal_at_or_after_freeze_cutoff(common_signal_time):
        return _freeze_rejection(
            strategy,
            {"generated_at": common_signal_time, "snapshot_id": snapshot_id},
        )
    try:
        rows, meta, version = _score_snapshot_strategy(
            provider, candidates, quotes, strategy, market, market_regime
        )
    except Exception as exc:
        return _snapshot_error_rejection(
            strategy=strategy,
            provider=provider,
            validation_store=validation_store,
            snapshot_id=snapshot_id,
            signal_time=common_signal_time,
            candidates=candidates,
            quotes=quotes,
            event_payload=event_payload,
            fundamental_payload=fundamental_payload,
            error=exc,
            market=market,
            market_regime=market_regime,
        )
    version = str(version or "").strip() or current_strategy_version(strategy)
    if not version:
        version = str(strategy)
    _attach_frozen_regime(rows, market_regime)
    meta["generated_at"] = common_signal_time
    meta["snapshot_id"] = snapshot_id
    scored_candidate_rows = meta.pop("_candidate_pool_rows", None)
    _attach_frozen_regime(scored_candidate_rows, market_regime)
    rows = attach_persisted_deepseek_features(
        rows,
        strategy,
        validation_store,
        signal_time=common_signal_time,
    )
    feature_count = sum(
        1
        for row in rows
        if str(row.get("deepseek_feature_status") or "") in {"precomputed", "abstain"}
    )
    feature_enabled = bool(getattr(config, "ENABLE_DEEPSEEK_FEATURES", True))
    deepseek_meta = {
        "enabled": feature_enabled,
        "status": (
            "precomputed_features"
            if feature_count
            else "local_only_no_precomputed_features"
            if feature_enabled
            else "precomputed_features_disabled"
        ),
        "strategy": strategy,
        "source": "validation_db_point_in_time_cache",
        "requested": len(rows),
        "reviewed": feature_count,
        "production_applied": False,
        "reason": (
            "快照只读取14:30前完成的DeepSeek结构化特征。"
            if feature_enabled
            else "DeepSeek结构化特征读取已关闭，保持本地基线。"
        ),
    }
    finalize_deepseek_meta(meta, rows, deepseek_meta)
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
        strategy_name=strategy,
        snapshot_id=snapshot_id,
    )
    for row in candidate_rows:
        row.setdefault("sample_type", SNAPSHOT_SAMPLE_TYPE)
        row.setdefault("sample_source", SNAPSHOT_SAMPLE_SOURCE)
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
    freeze_ready_at = datetime.now().isoformat(timespec="seconds")
    meta["freeze_ready_at"] = freeze_ready_at
    if _signal_at_or_after_freeze_cutoff(freeze_ready_at):
        return _freeze_rejection(strategy, meta)
    freeze_deadline = _freeze_deadline(common_signal_time)
    try:
        saved = validation_store.save_signals(
            strategy,
            version,
            meta["generated_at"],
            rows,
            candidate_rows=candidate_rows,
            batch_metadata={
                "data_source_timestamp": data_source_timestamp,
                "market_data_cutoff": meta["generated_at"],
                "generation": provenance,
                "snapshot_id": snapshot_id,
                "freeze_deadline": freeze_deadline,
                "sample_type": SNAPSHOT_SAMPLE_TYPE,
                "sample_source": SNAPSHOT_SAMPLE_SOURCE,
            },
            execution_policy=execution_policy,
        )
    except SignalFreezeDeadlineExceeded as exc:
        meta["freeze_completed_at"] = exc.observed_at
        return _freeze_rejection(strategy, meta)
    meta["freeze_completed_at"] = str(
        saved.get("freeze_transaction_checked_at") or datetime.now().isoformat(timespec="seconds")
    )
    meta["recommendation_frozen_at"] = meta["freeze_completed_at"]
    return {"ok": True, "strategy": strategy, "saved": saved, "meta": meta}


def run_snapshots(provider, validation_store, strategies: Iterable[str], market: str = "all") -> List[Dict[str, object]]:
    context = _prepare_snapshot_context(provider)
    if not context.get("snapshot_id"):
        context["snapshot_id"] = "snapshot_{}".format(uuid4().hex)
    if not context.get("snapshot_cutoff"):
        context["snapshot_cutoff"] = datetime.now().isoformat(timespec="seconds")
    results: List[Dict[str, object]] = []
    for strategy in strategies:
        try:
            results.append(
                run_snapshot(
                    provider,
                    validation_store,
                    strategy,
                    market=market,
                    _context=context,
                )
            )
        except Exception as exc:
            results.append(
                {
                    "ok": False,
                    "strategy": strategy,
                    "error": str(exc),
                    "saved": {"saved": 0, "replaced": 0},
                    "meta": {
                        "generated_at": str(context.get("signal_time") or context.get("snapshot_cutoff") or ""),
                        "snapshot_id": str(context.get("snapshot_id") or ""),
                        "rejection": "unhandled_exception",
                    },
                }
            )
    return results


def _snapshot_error_rejection(
    *,
    strategy: str,
    provider,
    validation_store,
    snapshot_id: str,
    signal_time: str,
    candidates,
    quotes,
    event_payload: Dict[str, object],
    fundamental_payload: Dict[str, object],
    error: object,
    market: str,
    market_regime: Dict[str, object],
    generation_status: str = "runtime_exception",
    reason: str = "snapshot_scoring_failed",
) -> Dict[str, object]:
    strategy = storage_strategy_name(strategy)
    error_text = str(error)
    generated_at = str(signal_time)
    provider_health = _provider_health(provider)
    quote_timestamp = ""
    if quotes is not None:
        try:
            quote_timestamp = str((quotes.attrs or {}).get("quote_timestamp") or "")
        except Exception:
            quote_timestamp = ""
    data_source_timestamp = str(
        quote_timestamp
        or provider_health.get("last_quote_refresh")
        or ""
    )
    candidate_rows: List[Dict[str, object]] = []
    try:
        candidate_rows = build_candidate_snapshot_rows(
            quotes,
            candidates,
            [],
            generated_at,
            event_payload=event_payload,
            fundamental_payload=fundamental_payload,
            provider_health=provider_health,
            strategy_name=strategy,
            snapshot_id=snapshot_id,
        )
        _attach_frozen_regime(candidate_rows, market_regime)
    except Exception:
        candidate_rows = []
    generation = {
        "status": str(generation_status or "runtime_exception"),
        "strategy": strategy,
        "reason": str(reason or "snapshot_scoring_failed"),
        "error": error_text,
    }
    execution_policy = build_execution_policy(strategy, market)
    version = str(current_strategy_version(strategy) or strategy)
    try:
        saved = validation_store.save_signals(
            strategy,
            version,
            generated_at,
            [],
            candidate_rows=candidate_rows,
            batch_metadata={
                "data_source_timestamp": data_source_timestamp,
                "market_data_cutoff": generated_at,
                "generation": generation,
                "snapshot_id": snapshot_id,
                "sample_type": SNAPSHOT_SAMPLE_TYPE,
                "sample_source": SNAPSHOT_SAMPLE_SOURCE,
            },
            execution_policy=execution_policy,
        )
    except SignalFreezeDeadlineExceeded as exc:
        return _freeze_rejection(
            strategy,
            {
                "generated_at": generated_at,
                "snapshot_id": snapshot_id,
                "error": str(exc),
                "rejection": "freeze_deadline_exceeded_during_error_batch",
            },
        )
    except Exception as exc:
        return {
            "ok": False,
            "strategy": strategy,
            "error": str(exc),
            "saved": {"saved": 0, "replaced": 0},
            "meta": {
                "generated_at": generated_at,
                "snapshot_id": snapshot_id,
                "rejection": "snapshot_error_save_failed",
                "original_error": error_text,
            },
        }
    return {
        "ok": False,
        "strategy": strategy,
        "saved": saved,
        "meta": {
            "generated_at": generated_at,
            "snapshot_id": snapshot_id,
            "rejection": str(generation_status or "runtime_exception"),
            "error": error_text,
        },
    }


def build_deepseek_precompute_rows(
    provider,
    strategies: Iterable[str],
    market: str = "all",
) -> Dict[str, object]:
    """Build the same local candidate pools used by snapshots, without freezing signals."""
    context = _prepare_snapshot_context(provider)
    if context.get("error"):
        return {"ok": False, "error": context["error"], "rows_by_strategy": {}}
    limit = max(1, int(getattr(config, "DEEPSEEK_FEATURE_REVIEW_LIMIT", 30)))
    rows_by_strategy: Dict[str, List[Dict[str, object]]] = {}
    meta_by_strategy: Dict[str, Dict[str, object]] = {}
    for raw_strategy in strategies or []:
        strategy = storage_strategy_name(raw_strategy)
        if strategy not in SNAPSHOT_STRATEGIES:
            continue
        rows, meta, version = _score_snapshot_strategy(
            provider,
            context["candidates"].copy(deep=True),
            context["quotes"],
            strategy,
            market,
            dict(context["market_regime"]),
        )
        candidate_pool = meta.get("_candidate_pool_rows") if isinstance(meta, dict) else None
        candidate_pool = candidate_pool if isinstance(candidate_pool, list) and candidate_pool else rows
        _attach_frozen_regime(candidate_pool, context["market_regime"])
        eligible = [
            dict(row)
            for row in candidate_pool or []
            if isinstance(row, dict) and row.get("eligible") is not False
        ]
        rows_by_strategy[strategy] = eligible[:limit]
        meta_by_strategy[strategy] = {
            "strategy_version": version,
            "candidate_count": len(candidate_pool or []),
            "selected_count": len(rows_by_strategy[strategy]),
        }
    rows_by_strategy, shared_count = _shared_deepseek_candidate_pool(rows_by_strategy)
    for strategy, meta in meta_by_strategy.items():
        meta["selected_count"] = len(rows_by_strategy.get(strategy) or [])
        meta["shared_candidate_count"] = shared_count
    return {
        "ok": True,
        "snapshot_id": str(context["snapshot_id"]),
        "cutoff_at": str(context["snapshot_cutoff"]),
        "rows_by_strategy": rows_by_strategy,
        "meta_by_strategy": meta_by_strategy,
    }


def _shared_deepseek_candidate_pool(rows_by_strategy: Dict[str, List[Dict[str, object]]]):
    limit = max(1, int(getattr(config, "DEEPSEEK_SHARED_RESEARCH_LIMIT", 24)))
    allocations = (("tomorrow_picks", 12), ("short_term", 8), ("swing_picks", 8))
    shared_codes = []
    for strategy, allocation in allocations:
        for row in (rows_by_strategy.get(strategy) or [])[:allocation]:
            code = normalize_code(row.get("code"))
            if code and code not in shared_codes:
                shared_codes.append(code)
            if len(shared_codes) >= limit:
                break
        if len(shared_codes) >= limit:
            break
    if len(shared_codes) < limit:
        for rows in rows_by_strategy.values():
            for row in rows or []:
                code = normalize_code(row.get("code"))
                if code and code not in shared_codes:
                    shared_codes.append(code)
                if len(shared_codes) >= limit:
                    break
            if len(shared_codes) >= limit:
                break
    allowed = set(shared_codes)
    limited = {
        strategy: [dict(row) for row in rows or [] if normalize_code(row.get("code")) in allowed]
        for strategy, rows in rows_by_strategy.items()
    }
    return limited, len(shared_codes)


def _prepare_snapshot_context(provider) -> Dict[str, object]:
    quotes = provider.get_realtime_quotes()
    freshness_error = _quote_freshness_error(provider, quotes)
    if freshness_error:
        return {"error": freshness_error}
    factor_quality_error = _snapshot_factor_quality_error(quotes)
    if factor_quality_error:
        return {"error": factor_quality_error}
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
    return {
        "quotes": quotes,
        "snapshot_cutoff": snapshot_cutoff,
        "snapshot_id": "snapshot_{}".format(uuid4().hex),
        "event_payload": event_payload,
        "fundamental_payload": fundamental_payload,
        "candidates": candidates,
        "market_regime": build_market_regime(candidates, breadth_source=quotes),
    }


def _snapshot_factor_quality_error(quotes) -> str:
    if quotes is None or quotes.empty:
        return "实时行情为空，拒绝生成验证快照。"
    minimum_ratio = min(
        1.0,
        max(0.0, float(getattr(config, "VALIDATION_REQUIRED_FACTOR_COVERAGE_RATIO", 0.99))),
    )
    if "price" in quotes.columns:
        active = quotes[pd.to_numeric(quotes["price"], errors="coerce").fillna(0) > 0]
    else:
        active = quotes
    if active.empty:
        return "实时行情没有有效价格，拒绝生成验证快照。"
    degraded = []
    for field in SNAPSHOT_REQUIRED_FACTOR_FIELDS:
        if field not in active.columns:
            coverage = 0.0
        else:
            coverage = float(pd.to_numeric(active[field], errors="coerce").notna().mean())
        if coverage < minimum_ratio:
            degraded.append("{}={:.1%}".format(field, coverage))
    if not degraded:
        return ""
    return "快照关键评分字段覆盖不足（最低 {:.0%}）：{}；拒绝把缺失值当 0 写入验证样本。".format(
        minimum_ratio,
        ", ".join(degraded),
    )


def _attach_frozen_regime(rows, market_regime: Dict[str, object]) -> None:
    if not isinstance(rows, list):
        return
    regime = dict(market_regime or {})
    level = str(regime.get("level") or "unknown")
    label = str(regime.get("label") or level)
    for row in rows:
        if isinstance(row, dict):
            row["market_regime"] = level
            row["market_regime_label"] = label


def _signal_at_or_after_freeze_cutoff(signal_time: str) -> bool:
    text = str(signal_time or "")
    clock = text.split("T", 1)[1][:5] if "T" in text else ""
    cutoff = str(getattr(config, "RECOMMENDATION_FREEZE_CUTOFF_TIME", "14:50"))[:5]
    return bool(clock and cutoff and clock >= cutoff)


def _freeze_deadline(signal_time: str) -> str:
    signal_date = str(signal_time or "")[:10]
    cutoff = str(getattr(config, "RECOMMENDATION_FREEZE_CUTOFF_TIME", "14:50"))[:5]
    return "{}T{}:00".format(signal_date, cutoff) if signal_date and cutoff else ""


def _freeze_rejection(strategy: str, meta: Dict[str, object]) -> Dict[str, object]:
    return {
        "ok": False,
        "strategy": strategy,
        "error": "{}未能在{}前完成冻结，拒绝保存当日推荐批次。".format(
            strategy,
            getattr(config, "RECOMMENDATION_FREEZE_CUTOFF_TIME", "14:50")
        ),
        "saved": {"saved": 0, "replaced": 0},
        "meta": meta,
    }


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


def _quote_freshness_error(provider, quotes) -> str:
    if quotes is None or quotes.empty:
        return "行情为空，拒绝保存推荐快照。"
    min_rows = int(getattr(config, "QUOTE_SNAPSHOT_MIN_ROWS", 50))
    if len(quotes) < min_rows:
        return "行情行数不足 {} 条，拒绝保存推荐快照。".format(min_rows)
    health_fn = getattr(provider, "health", None)
    if not callable(health_fn):
        return ""
    health = health_fn() or {}
    source = str(health.get("quotes_source") or "")
    if not source or source == "unavailable":
        return "行情来源不可用，拒绝保存推荐快照。"
    if "快照" in source and not getattr(config, "VALIDATION_ALLOW_LOCAL_QUOTE_SNAPSHOT", False):
        return "当前行情来自本地快照，拒绝保存为今日真实推荐。"
    refreshed = health.get("last_quote_refresh")
    if not refreshed:
        return "缺少行情刷新时间，拒绝保存推荐快照。"
    try:
        refreshed_at = datetime.fromisoformat(str(refreshed))
    except ValueError:
        return "行情刷新时间格式异常，拒绝保存推荐快照。"
    max_age = int(getattr(config, "VALIDATION_SNAPSHOT_MAX_QUOTE_AGE_SECONDS", 900))
    age = (datetime.now() - refreshed_at).total_seconds()
    if age > max_age:
        return "行情已超过 {} 秒未刷新，拒绝保存推荐快照。".format(max_age)
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
