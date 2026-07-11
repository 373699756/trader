import threading
import time
from typing import Dict, List

import pandas as pd

from . import config
from .daily_data import load_history_frames
from .event_risk import attach_event_risk, load_event_risk
from .factors import ALPHALITE_COLUMNS, ALPHALITE_META_COLUMNS, build_alphalite_factors, merge_alphalite
from .fundamentals import attach_fundamental_factors, load_fundamentals
from .meta_labeling import apply_meta_labeling, train_meta_label_model
from .normalization import coerce_number
from .normalization import normalize_code, rename_known_columns
from .probability_calibration import apply_score_calibration, load_calibrator, train_score_calibrator
from .risk_blacklist import attach_risk_blacklist, load_risk_blacklist
from .scoring import mark_backup_watch, prepare_candidates
from .sentiment import score_stock_sentiment
from .strategy_health import strategy_status
from .strategy_validation import StrategyValidationStore, _primary_return_config, validation_baseline_config

_SENTIMENT_CACHE_LOCK = threading.Lock()
_HISTORY_REFRESH_LOCK = threading.Lock()
_HISTORY_REFRESHING = set()


def load_local_history_frames(codes, days: int = 90) -> Dict[str, pd.DataFrame]:
    try:
        return load_history_frames(config.MARKET_DATA_DB_PATH, codes, days=days)
    except Exception:
        return {}


def candidate_code_rows(provider, quotes_cache, limit: int) -> list:
    quotes = quotes_cache.get()
    if quotes is None:
        quotes = provider.get_realtime_quotes()
        quotes_cache.set(quotes)
    candidates = attach_event_risk(prepare_candidates(quotes), load_event_risk(provider))
    candidates = attach_risk_blacklist(candidates, load_risk_blacklist())
    codes = candidates["code"].tolist() if candidates is not None and "code" in candidates.columns else []
    candidates = attach_fundamental_factors(candidates, load_fundamentals(provider, codes=codes))
    if candidates.empty:
        return []
    sort_columns = [column for column in ("pct_chg", "turnover") if column in candidates.columns]
    if sort_columns:
        candidates = candidates.sort_values(sort_columns, ascending=False)
    rows = []
    for index, row in candidates.head(max(1, int(limit))).reset_index(drop=True).iterrows():
        rows.append(
            {
                "code": row.get("code", ""),
                "name": row.get("name", ""),
                "signal_count": 0,
                "latest_signal_date": "",
                "best_rank": index + 1,
            }
        )
    return rows


def quote_lookup(quotes) -> Dict[str, Dict[str, object]]:
    if quotes is None or quotes.empty:
        return {}
    try:
        df = rename_known_columns(quotes.copy())
    except Exception:
        df = quotes.copy()
    if "code" not in df.columns:
        return {}
    df["code"] = df["code"].map(normalize_code)
    lookup = {}
    for _, row in df.iterrows():
        lookup[str(row.get("code"))] = row.to_dict()
    return lookup


def stock_exists_in_quotes(code: str, quotes) -> bool:
    if quotes is None or quotes.empty:
        return False
    try:
        df = rename_known_columns(quotes.copy())
    except Exception:
        df = quotes.copy()
    if "code" not in df.columns:
        return False
    return normalize_code(code) in set(df["code"].map(normalize_code).astype(str))


def sentiment_for_candidates(provider, cache, candidates) -> Dict[str, Dict[str, object]]:
    if not config.ENABLE_INLINE_SENTIMENT:
        return {}
    lookup: Dict[str, Dict[str, object]] = {}
    state = _sentiment_cache_state(cache)
    now = time.time()
    pending = []
    for item in candidates[:30]:
        code = normalize_code(item.get("code"))
        if not code:
            continue
        entry = state["entries"].get(code) or {}
        value = entry.get("value")
        expires_at = float(entry.get("expires_at") or 0.0)
        if isinstance(value, dict):
            lookup[code] = dict(value)
            if expires_at <= now:
                pending.append({"code": code, "name": item.get("name", "")})
            continue
        lookup[code] = _default_sentiment("舆情刷新中")
        pending.append({"code": code, "name": item.get("name", "")})
    _schedule_sentiment_refresh(provider, cache, pending)
    if cache is not None:
        cache.set(state)
    return lookup


def attach_alphalite_factors(provider, cache, candidates):
    if not config.ENABLE_HISTORY_FACTORS or config.HISTORY_FACTOR_LIMIT <= 0:
        return candidates
    history_by_code = {}
    target_codes = candidates.sort_values(["pct_chg", "turnover"], ascending=False).head(
        config.HISTORY_FACTOR_LIMIT
    )["code"].tolist()
    history_by_code.update(load_local_history_frames(target_codes, days=90))
    max_request_fetches = max(0, int(getattr(config, "HISTORY_FACTORS_MAX_REQUEST_FETCHES", 8)))
    fetch_on_request = bool(getattr(config, "HISTORY_FACTORS_FETCH_ON_REQUEST", False))
    missing_codes = []
    for code in target_codes:
        if code in history_by_code:
            continue
        try:
            if hasattr(provider, "get_cached_history"):
                history = provider.get_cached_history(code, days=90)
            else:
                history = None
        except Exception:
            continue
        if history is not None and not history.empty:
            history_by_code[code] = history
        else:
            missing_codes.append(code)
    if fetch_on_request and max_request_fetches > 0:
        _schedule_history_factor_refresh(provider, missing_codes[:max_request_fetches])
    factors = _cached_alphalite_factors(cache, history_by_code)
    return merge_alphalite(candidates, factors)


def _schedule_history_factor_refresh(provider, codes) -> None:
    queued = []
    with _HISTORY_REFRESH_LOCK:
        for value in codes or []:
            code = normalize_code(value)
            if not code or code in _HISTORY_REFRESHING:
                continue
            _HISTORY_REFRESHING.add(code)
            queued.append(code)
    if not queued:
        return
    worker = threading.Thread(
        target=_refresh_history_factor_entries,
        args=(provider, queued),
        name="history-factor-refresh",
        daemon=True,
    )
    worker.start()


def _refresh_history_factor_entries(provider, codes) -> None:
    try:
        if hasattr(provider, "prefetch_history"):
            provider.prefetch_history(codes, days=90)
        else:
            for code in codes:
                try:
                    provider.get_history(code, days=90)
                except Exception:
                    continue
    except Exception as exc:
        recorder = getattr(provider, "_record_sentiment_error", None)
        if callable(recorder):
            try:
                recorder("后台历史因子刷新失败: {}".format(exc))
            except Exception:
                pass
    finally:
        with _HISTORY_REFRESH_LOCK:
            for code in codes:
                _HISTORY_REFRESHING.discard(normalize_code(code))


def attach_alphalite_factors_for_codes(provider, candidates, codes):
    if not config.ENABLE_HISTORY_FACTORS:
        return candidates
    target = {normalize_code(code) for code in codes if code}
    if not target:
        return candidates
    if candidates is None or candidates.empty or "code" not in candidates.columns:
        return candidates
    target &= set(candidates["code"].astype(str).tolist())
    if not target:
        return candidates
    history_by_code = load_local_history_frames(target, days=90)
    for code in target:
        if code in history_by_code:
            continue
        try:
            history = provider.get_history(code, days=90)
        except Exception:
            continue
        if history is not None and not history.empty:
            history_by_code[code] = history
    if not history_by_code:
        return candidates
    return merge_alphalite(candidates, build_alphalite_factors(history_by_code))


def _history_signature(history: pd.DataFrame) -> str:
    if history is None or history.empty:
        return ""
    df = rename_known_columns(history.copy())
    if "trade_date" not in df.columns:
        for candidate in ("日期", "date", "Date", "交易日期"):
            if candidate in df.columns:
                df["trade_date"] = df[candidate]
                break
    if "trade_date" not in df.columns or df.empty:
        return ""
    return str(df["trade_date"].iloc[-1]).strip().replace("-", "")


def _empty_alphalite_row(code: str) -> Dict[str, object]:
    row = {"code": normalize_code(code)}
    for column in ALPHALITE_COLUMNS + ALPHALITE_META_COLUMNS:
        row[column] = 0.0
    return row


def _cached_alphalite_factors(cache, history_by_code: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    if not history_by_code:
        return pd.DataFrame(columns=("code",) + ALPHALITE_COLUMNS + ALPHALITE_META_COLUMNS)
    factor_cache = cache.get() if cache is not None else None
    if not isinstance(factor_cache, dict):
        factor_cache = {}
    rows = []
    pending_history: Dict[str, pd.DataFrame] = {}
    for code, history in history_by_code.items():
        normalized = normalize_code(code)
        signature = _history_signature(history)
        cached = factor_cache.get(normalized)
        if cached and cached.get("signature") == signature and isinstance(cached.get("row"), dict):
            rows.append(dict(cached["row"]))
            continue
        pending_history[normalized] = history
    if pending_history:
        fresh = build_alphalite_factors(pending_history)
        fresh_rows = {}
        if fresh is not None and not fresh.empty:
            for _, row in fresh.iterrows():
                item = row.to_dict()
                code = normalize_code(item.get("code"))
                if code:
                    fresh_rows[code] = item
        for code, history in pending_history.items():
            row = fresh_rows.get(code) or _empty_alphalite_row(code)
            factor_cache[code] = {
                "signature": _history_signature(history),
                "row": row,
            }
            rows.append(dict(row))
        if cache is not None:
            cache.set(factor_cache)
    if not rows:
        return pd.DataFrame(columns=("code",) + ALPHALITE_COLUMNS + ALPHALITE_META_COLUMNS)
    return pd.DataFrame(rows)


def _default_sentiment(summary: str = "舆情接口暂不可用") -> Dict[str, object]:
    return {"score": 50.0, "summary": summary, "risk_words": [], "trigger_words": [], "items": []}


def _sentiment_cache_state(cache) -> Dict[str, object]:
    cached = cache.get() if cache is not None else None
    if not isinstance(cached, dict):
        return {"entries": {}, "refreshing": set()}
    entries = cached.get("entries")
    refreshing = cached.get("refreshing")
    return {
        "entries": entries if isinstance(entries, dict) else {},
        "refreshing": refreshing if isinstance(refreshing, set) else set(),
    }


def _schedule_sentiment_refresh(provider, cache, candidates) -> None:
    if not candidates:
        return
    with _SENTIMENT_CACHE_LOCK:
        state = _sentiment_cache_state(cache)
        queued = []
        for item in candidates:
            code = normalize_code(item.get("code"))
            if not code or code in state["refreshing"]:
                continue
            state["refreshing"].add(code)
            queued.append({"code": code, "name": item.get("name", "")})
        if not queued:
            if cache is not None:
                cache.set(state)
            return
        if cache is not None:
            cache.set(state)
    worker = threading.Thread(
        target=_refresh_sentiment_entries,
        args=(provider, cache, queued),
        name="sentiment-refresh",
        daemon=True,
    )
    worker.start()


def _refresh_sentiment_entries(provider, cache, candidates) -> None:
    ttl_seconds = max(30, int(getattr(cache, "ttl_seconds", 0) or 0))
    now = time.time()
    refreshed = {}
    for item in candidates:
        code = normalize_code(item.get("code"))
        if not code:
            continue
        try:
            value = score_stock_sentiment(provider, code, name=item.get("name", ""))
        except Exception:
            value = _default_sentiment()
        refreshed[code] = {
            "value": value,
            "expires_at": now + ttl_seconds,
        }
    with _SENTIMENT_CACHE_LOCK:
        state = _sentiment_cache_state(cache)
        for code, entry in refreshed.items():
            state["entries"][code] = entry
            state["refreshing"].discard(code)
        for item in candidates:
            code = normalize_code(item.get("code"))
            if code:
                state["refreshing"].discard(code)
        if cache is not None:
            cache.set(state)


def attach_validation_summary(
    rows: list,
    validation_store: StrategyValidationStore,
    strategy_name: str,
    days: int = 20,
    metrics_fn=None,
) -> None:
    metrics = metrics_fn(strategy_name, days) if metrics_fn else validation_store.metrics(strategy_name, days=days)
    sample_count = int(metrics.get("sample_count") or 0)
    validation_baseline = metrics.get("validation_baseline") or validation_baseline_config(strategy_name)
    summary = {
        "strategy_name": strategy_name,
        "days": days,
        "sample_count": sample_count,
        "real_sample_count": metrics.get("real_sample_count", 0),
        "replay_sample_count": metrics.get("replay_sample_count", 0),
        "win_rate_next_close": metrics.get("win_rate_next_close"),
        "win_rate_primary_net": metrics.get("win_rate_primary_net"),
        "avg_primary_return_net": metrics.get("avg_primary_return_net"),
        "real_win_rate_primary_net": metrics.get("real_win_rate_primary_net"),
        "real_avg_primary_return_net": metrics.get("real_avg_primary_return_net"),
        "real_avg_primary_return_net_ci95_low": metrics.get("real_avg_primary_return_net_ci95_low"),
        "real_avg_primary_return_net_ci95_high": metrics.get("real_avg_primary_return_net_ci95_high"),
        "real_win_rate_primary_net_ci95_low": metrics.get("real_win_rate_primary_net_ci95_low"),
        "real_portfolio_max_drawdown_pct": metrics.get("real_portfolio_max_drawdown_pct"),
        "primary_horizon_label": metrics.get("primary_horizon_label"),
        "validation_baseline": validation_baseline,
        "validation_baseline_id": metrics.get("validation_baseline_id") or validation_baseline.get("baseline_id"),
        "current_baseline_outcome_count": metrics.get("current_baseline_outcome_count", 0),
        "raw_outcome_sample_count": metrics.get("raw_outcome_sample_count", 0),
        "legacy_baseline_outcome_count": metrics.get("legacy_baseline_outcome_count", 0),
        "excluded_baseline_mismatch_count": metrics.get("excluded_baseline_mismatch_count", 0),
        "avg_trade_cost_pct": metrics.get("avg_trade_cost_pct"),
        "survivorship_corrected_count": metrics.get("survivorship_corrected_count", 0),
        "hit_3pct_rate": metrics.get("hit_3pct_rate"),
        "avg_next_close_return": metrics.get("avg_next_close_return"),
        "avg_max_drawdown_3d": metrics.get("avg_max_drawdown_3d"),
        "label": "暂无验证样本" if sample_count <= 0 else "过去同类信号",
    }
    for row in rows:
        row["similar_signal_stats"] = summary
    attach_score_calibration(rows, validation_store, strategy_name, days=max(60, days))
    attach_meta_labeling(rows, validation_store, strategy_name, days=max(120, days))


def _bucket_label(score: float, edges: List[float]) -> str:
    low = 0
    for high in edges:
        if score < high:
            return f"{int(low)}-{int(high)}"
        low = high
    return f"{int(edges[-1])}+"


def _score_bucket_stats(samples: List[Dict[str, object]], value_getter, return_key: str) -> Dict[str, Dict[str, object]]:
    edges = [45.0, 55.0, 65.0, 75.0, 101.0]
    buckets: Dict[str, Dict[str, object]] = {}
    for sample in samples:
        value = coerce_number(value_getter(sample), None)
        if value is None:
            continue
        label = _bucket_label(value, edges)
        bucket = buckets.setdefault(
            label,
            {
                "sample_count": 0,
                "win_count": 0,
                "return_total": 0.0,
                "drawdown_total": 0.0,
            },
        )
        bucket["sample_count"] += 1
        primary_return = coerce_number(sample.get(return_key))
        drawdown = coerce_number(sample.get("max_drawdown"))
        if primary_return > 0:
            bucket["win_count"] += 1
        bucket["return_total"] += primary_return
        bucket["drawdown_total"] += drawdown
    result: Dict[str, Dict[str, object]] = {}
    for label, bucket in buckets.items():
        sample_count = int(bucket["sample_count"] or 0)
        if sample_count <= 0:
            continue
        result[label] = {
            "label": label,
            "sample_count": sample_count,
            "win_rate": round(bucket["win_count"] * 100.0 / sample_count, 2),
            "avg_return": round(bucket["return_total"] / sample_count, 4),
            "avg_drawdown": round(bucket["drawdown_total"] / sample_count, 4),
        }
    return result


def attach_score_calibration(
    rows: list,
    validation_store: StrategyValidationStore,
    strategy_name: str,
    days: int = 60,
) -> None:
    if not rows:
        return
    try:
        samples = validation_store.live_weight_samples(strategy_name, days=days)
    except Exception:
        return
    if not samples:
        return
    calibrator = load_calibrator(strategy_name)
    if not calibrator.is_fitted:
        calibrator = train_score_calibrator(strategy_name, samples)
    apply_score_calibration(rows, calibrator)
    decision_buckets = _score_bucket_stats(
        samples,
        lambda sample: (sample.get("raw") or {}).get("decision_score", sample.get("stored_score")),
        "primary_return_net",
    )
    sell_buckets = _score_bucket_stats(
        samples,
        lambda sample: ((sample.get("raw") or {}).get("sell_risk") or {}).get(
            "score",
            ((sample.get("raw") or {}).get("serenity_profile") or {}).get("risk_score"),
        ),
        "primary_return_net",
    )
    for row in rows:
        decision_value = coerce_number(row.get("decision_score"), row.get("score"))
        sell_risk = row.get("sell_risk") or {}
        sell_value = coerce_number(sell_risk.get("score"), ((row.get("serenity_profile") or {}).get("risk_score")))
        decision_label = _bucket_label(decision_value, [45.0, 55.0, 65.0, 75.0, 101.0]) if decision_value is not None else ""
        sell_label = _bucket_label(sell_value, [45.0, 55.0, 65.0, 75.0, 101.0]) if sell_value is not None else ""
        if decision_label and decision_label in decision_buckets:
            row["decision_calibration"] = decision_buckets[decision_label]
        if sell_label and sell_label in sell_buckets:
            row["sell_risk_calibration"] = sell_buckets[sell_label]


def attach_meta_labeling(
    rows: list,
    validation_store: StrategyValidationStore,
    strategy_name: str,
    days: int = 120,
) -> None:
    if not rows:
        return
    try:
        samples = validation_store.live_weight_samples(strategy_name, days=days)
    except Exception:
        return
    if not samples:
        return
    model = train_meta_label_model(strategy_name, samples)
    if not model.get("is_fitted"):
        return
    enforce = bool(getattr(config, "ENABLE_META_LABELING", False)) and bool(
        getattr(config, "META_LABELING_ENFORCE_ACTION", False)
    )
    apply_meta_labeling(rows, model, enforce=enforce)


def validation_batch_summary(rows: List[Dict[str, object]], strategy_name: str) -> Dict[str, object]:
    primary_column, primary_days, primary_label = _primary_return_config(strategy_name)
    valid_returns = []
    pending = 0
    skipped = 0
    for row in rows or []:
        if row.get("skip_reason"):
            skipped += 1
            continue
        if not row.get("outcome_updated_at"):
            pending += 1
            continue
        raw_return = row.get(primary_column)
        if raw_return is None:
            pending += 1
            continue
        value = coerce_number(raw_return, None)
        if value is None:
            pending += 1
            continue
        trade_cost = coerce_number(row.get("trade_cost_pct"), 0.0)
        valid_returns.append(round(value - trade_cost, 4))
    sample = len(valid_returns)
    up = sum(1 for value in valid_returns if value > 0)
    down = sum(1 for value in valid_returns if value < 0)
    flat = sample - up - down
    avg_return = round(sum(valid_returns) / sample, 4) if sample else None
    win_rate = round(up / sample * 100, 4) if sample else None
    return {
        "strategy": strategy_name,
        "primary_return_field": primary_column,
        "primary_holding_days": primary_days,
        "primary_horizon_label": primary_label,
        "sample_count": sample,
        "up_count": up,
        "down_count": down,
        "flat_count": flat,
        "pending_count": pending,
        "skipped_count": skipped,
        "win_rate": win_rate,
        "avg_return": avg_return,
    }


def strategy_validation_gate_decision(
    metrics: Dict[str, object],
    strategy_name: str = "",
) -> Dict[str, object]:
    blocked_scale = {
        "position_scale": 0.0,
        "position_scale_reason": "验证未通过，仓位归零",
    }
    if not metrics:
        return {
            "blocked": True,
            "allows_backup": True,
            "validated": False,
            "reason": "验证样本不足，暂不形成可执行推荐，仅保留备选观察",
            "state": "pending",
            **blocked_scale,
        }
    strategy_name = str(strategy_name or metrics.get("strategy_name") or "")
    status = strategy_status(metrics)
    primary_outcomes = int(metrics.get("outcome_sample_count") or metrics.get("sample_count") or 0)
    avg_net = coerce_number(metrics.get("avg_primary_return_net"))
    win_net = coerce_number(metrics.get("win_rate_primary_net"))
    real_avg_net = coerce_number(metrics.get("real_avg_primary_return_net"))
    real_win_net = coerce_number(metrics.get("real_win_rate_primary_net"))
    drawdown_value = metrics.get("real_portfolio_max_drawdown_pct")
    if drawdown_value is None:
        drawdown_value = metrics.get("real_avg_max_drawdown_primary")
    if drawdown_value is None:
        drawdown_value = metrics.get("avg_max_drawdown_primary", metrics.get("avg_max_drawdown_3d"))
    avg_drawdown = coerce_number(drawdown_value)
    decision = {
        "blocked": False,
        "allows_backup": True,
        "validated": False,
        "reason": "",
        "state": status.get("state", "unknown"),
        "label": status.get("label", ""),
        "outcome_sample_count": primary_outcomes,
        "total_outcome_sample_count": int(metrics.get("total_outcome_sample_count") or 0),
        "avg_primary_return_net": avg_net,
        "win_rate_primary_net": win_net,
        "real_sample_count": int(metrics.get("real_sample_count") or 0),
        "real_day_count": int(metrics.get("real_day_count") or 0),
        "real_avg_primary_return_net": real_avg_net,
        "real_win_rate_primary_net": real_win_net,
        "real_avg_primary_return_net_ci95_low": metrics.get("real_avg_primary_return_net_ci95_low"),
        "real_win_rate_primary_net_ci95_low": metrics.get("real_win_rate_primary_net_ci95_low"),
        "real_portfolio_max_drawdown_pct": metrics.get("real_portfolio_max_drawdown_pct"),
        "avg_max_drawdown_primary": avg_drawdown,
        "position_scale": 1.0,
        "position_scale_reason": "验证通过，标准仓位",
    }
    if status.get("state") == "retired":
        decision["blocked"] = True
        decision["reason"] = "验证退场：真实交易日净收益、净胜率或主周期回撤不达标，暂停执行，允许备选观察"
        decision["position_scale"] = 0.0
        decision["position_scale_reason"] = "策略已退场，仓位归零"
        return decision
    min_real_days = int(
        getattr(
            config,
            "STRATEGY_DECAY_MIN_REAL_DAYS",
            getattr(config, "STRATEGY_DECAY_MIN_REAL_SAMPLES", 20),
        )
    )
    real_days = int(metrics.get("real_day_count") or 0)
    if real_days < min_real_days:
        decision["blocked"] = True
        decision["reason"] = "真实验证不足{}个交易日，暂不形成可执行推荐，仅保留备选观察".format(
            min_real_days
        )
        decision["position_scale"] = 0.0
        decision["position_scale_reason"] = "真实验证不足，仓位归零"
        return decision
    min_win_rate = coerce_number(getattr(config, "STRATEGY_VALIDATION_MIN_WIN_RATE", 50.0), 50.0)
    ci_low_raw = metrics.get("real_avg_primary_return_net_ci95_low")
    ci_low = coerce_number(ci_low_raw) if ci_low_raw is not None else None
    drawdown_floor = coerce_number(
        getattr(config, "STRATEGY_VALIDATION_MAX_AVG_DRAWDOWN_PCT", -8.0),
        -8.0,
    )
    if real_days >= min_real_days and (
        real_avg_net <= 0
        or real_win_net < min_win_rate
        or avg_drawdown <= drawdown_floor
        or (
            bool(getattr(config, "STRATEGY_VALIDATION_REQUIRE_POSITIVE_CI", True))
            and ci_low is not None
            and ci_low <= 0
        )
    ):
        decision["blocked"] = True
        decision["reason"] = "验证门控：最近真实交易日组合净表现或回撤不达标，仅保留备选观察"
        decision["position_scale"] = 0.0
        decision["position_scale_reason"] = "验证指标不达标，仓位归零"
        return decision
    decision["validated"] = True
    scale_info = dynamic_position_scaling(metrics, status)
    decision.update(scale_info)
    return decision


def dynamic_position_scaling(metrics: Dict[str, object], status: Dict[str, object] = None) -> Dict[str, object]:
    if not bool(getattr(config, "ENABLE_DYNAMIC_POSITION_SCALING", True)):
        return {"position_scale": 1.0, "position_scale_reason": "动态仓位缩放关闭"}
    metrics = metrics or {}
    status = status or strategy_status(metrics)
    min_scale = max(0.0, min(1.0, coerce_number(getattr(config, "STRATEGY_POSITION_SCALE_MIN", 0.35), 0.35)))
    probation_scale = max(min_scale, min(1.0, coerce_number(getattr(config, "STRATEGY_POSITION_SCALE_PROBATION", 0.6), 0.6)))
    if status.get("state") == "retired":
        return {"position_scale": 0.0, "position_scale_reason": "策略已退场，仓位归零"}
    scale = 1.0
    reasons = []
    state = str(status.get("state") or "")
    if state == "probation":
        scale = min(scale, probation_scale)
        reasons.append("策略处于观察降权")
    win_rate = coerce_number(metrics.get("real_win_rate_primary_net"), metrics.get("win_rate_primary_net"))
    min_win_rate = coerce_number(getattr(config, "STRATEGY_VALIDATION_MIN_WIN_RATE", 50.0), 50.0)
    if win_rate < min_win_rate + 2.0:
        scale = min(scale, max(min_scale, 0.75))
        reasons.append("净胜率接近门槛")
    avg_net = coerce_number(metrics.get("real_avg_primary_return_net"), metrics.get("avg_primary_return_net"))
    if avg_net < 0.3:
        scale = min(scale, max(min_scale, 0.8))
        reasons.append("平均净收益偏薄")
    ci_low_raw = metrics.get("real_avg_primary_return_net_ci95_low")
    if ci_low_raw is not None and coerce_number(ci_low_raw) < 0.2:
        scale = min(scale, max(min_scale, 0.75))
        reasons.append("收益置信下界偏低")
    drawdown_value = metrics.get("real_portfolio_max_drawdown_pct")
    if drawdown_value is None:
        drawdown_value = metrics.get("real_avg_max_drawdown_primary")
    if drawdown_value is None:
        drawdown_value = metrics.get("avg_max_drawdown_primary", metrics.get("avg_max_drawdown_3d"))
    drawdown = coerce_number(drawdown_value)
    drawdown_floor = coerce_number(getattr(config, "STRATEGY_VALIDATION_MAX_AVG_DRAWDOWN_PCT", -8.0), -8.0)
    if drawdown < drawdown_floor * 0.75:
        scale = min(scale, max(min_scale, 0.7))
        reasons.append("回撤接近门槛")
    scale = max(min_scale, min(1.0, scale))
    if not reasons:
        reasons.append("策略表现稳健")
    return {
        "position_scale": round(scale, 4),
        "position_scale_reason": "，".join(reasons),
    }


def validation_gate_window_days() -> int:
    min_real_days = int(
        getattr(
            config,
            "STRATEGY_DECAY_MIN_REAL_DAYS",
            getattr(config, "STRATEGY_DECAY_MIN_REAL_SAMPLES", 60),
        )
    )
    configured = int(getattr(config, "STRATEGY_VALIDATION_GATE_WINDOW_DAYS", 120))
    return max(min_real_days, configured)


def tomorrow_validation_gate_decision(metrics: Dict[str, object]) -> Dict[str, object]:
    return strategy_validation_gate_decision(metrics, "tomorrow_picks")


def apply_strategy_validation_gate(
    strategy_name: str,
    rows: List[Dict[str, object]],
    meta: Dict[str, object],
    metrics: Dict[str, object],
) -> Dict[str, object]:
    decision = strategy_validation_gate_decision(metrics, strategy_name)
    if meta is not None:
        meta["validation_gate"] = decision
    if decision.get("blocked"):
        demote_strategy_rows_to_backup(
            strategy_name,
            rows,
            meta,
            decision.get("reason") or "真实验证不达标，暂停执行",
        )
    else:
        apply_position_scale(rows, decision.get("position_scale", 1.0), decision.get("position_scale_reason", ""))
    return decision


def apply_tomorrow_validation_gate(
    rows: List[Dict[str, object]],
    meta: Dict[str, object],
    metrics: Dict[str, object],
) -> Dict[str, object]:
    return apply_strategy_validation_gate("tomorrow_picks", rows, meta, metrics)


def demote_strategy_rows_to_backup(
    strategy_name: str,
    rows: List[Dict[str, object]],
    meta: Dict[str, object],
    reason: str,
) -> None:
    for row in rows or []:
        label = "盘中观察" if row.get("observation_mode") == "intraday_provisional" else "备选观察"
        mark_backup_watch(row, label=label, reason=reason)
    if meta is not None:
        meta["primary_watch_count"] = 0
        meta["backup_watch_count"] = len(rows or [])
        meta["primary_gate_count"] = 0
        current_reason = str(meta.get("gate_reason") or "").strip()
        meta["gate_reason"] = "{} {}".format(current_reason, reason).strip()
        meta["validation_strategy"] = strategy_name


def apply_position_scale(rows: List[Dict[str, object]], scale, reason: str = "") -> None:
    factor = max(0.0, min(1.0, coerce_number(scale, 1.0)))
    for row in rows or []:
        trade_action = row.get("trade_action")
        if not isinstance(trade_action, dict):
            continue
        original = coerce_number(trade_action.get("position_size"), 0.0)
        trade_action.setdefault("base_position_size", original)
        trade_action["position_scale"] = round(factor, 4)
        trade_action["position_size"] = round(original * factor, 4)
        if reason and factor < 0.999 and original > 0:
            trade_action["scale_reason"] = reason


def demote_tomorrow_rows_to_backup(
    rows: List[Dict[str, object]],
    meta: Dict[str, object],
    reason: str,
) -> None:
    demote_strategy_rows_to_backup("tomorrow_picks", rows, meta, reason)


def market_news(provider, cache) -> List[Dict[str, object]]:
    if not config.ENABLE_MARKET_NEWS:
        return []
    cached = cache.get()
    if cached is not None:
        return cached
    try:
        news = provider.get_market_news(limit=80)
    except Exception:
        news = []
    cache.set(news)
    return news
