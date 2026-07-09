from datetime import datetime
import json
import os
import threading
import time
from typing import Dict, List, Tuple

from flask import Flask, Response, jsonify, render_template, request, stream_with_context
import pandas as pd

from . import config
from .app_runtime_support import (
    deepseek_stock_prediction_review,
    deepseek_validation_review,
    risk_blacklist_summary,
)
from .app_response_support import (
    error_payload,
    response_payload,
    saved_tomorrow_fallback_payload,
    snapshot_fallback_payload,
)
from .backtest import parse_code_list, run_alphalite_backtest, run_rolling_alphalite_backtest
from .daily_data import list_market_data_codes
from .deepseek_client import review_strategy_validation  # compatibility alias for existing tests/local imports
from .event_risk import attach_event_risk, load_event_risk
from .factor_ic import load_factor_ic
from .fundamentals import attach_fundamental_factors, load_fundamentals
from .providers import MarketDataProvider, TimedCache
from .prediction import build_stock_prediction
from .recommendation_runtime_support import (
    build_recommendation_horizons,
    finalize_recommendation_payload_meta,
    prediction_strategy_rows,
    scored_strategy_rows,
)
from .recommendation_snapshot import load_recommendation_snapshot, save_recommendation_snapshot
from .risk_blacklist import attach_risk_blacklist, load_risk_blacklist
from .normalization import coerce_number, normalize_code
from .selfcheck import factor_coverage
from .scoring import (
    build_market_regime,
    candidate_filter_report,
    prepare_candidates,
)
from .app_support import (
    attach_alphalite_factors,
    attach_alphalite_factors_for_codes,
    apply_tomorrow_validation_gate,
    attach_validation_summary,
    candidate_code_rows,
    load_local_history_frames,
    market_news as fetch_market_news,
    quote_lookup,
    sentiment_for_candidates,
    stock_exists_in_quotes,
    validation_batch_summary,
)
from .strategies import storage_strategy_name
from .sentiment import build_market_sentiment_index
from .strategy_validation import StrategyValidationStore
from .snapshot import SNAPSHOT_STRATEGIES, run_snapshot, run_snapshots
from .validation_replay import backfill_strategy_validation_samples
from .stability import TopKDropoutTracker
from .validation_runtime_support import (
    analysis_window,
    auto_snapshot_time_parts,
    configured_auto_snapshot_strategies,
    next_auto_snapshot_at,
    next_auto_update_window_start,
    run_validation_auto_snapshot_once as run_validation_auto_snapshot_once_support,
    run_validation_auto_update_once as run_validation_auto_update_once_support,
    run_validation_tuning_once as run_validation_tuning_once_support,
    set_status,
    start_validation_auto_snapshot_worker,
    start_validation_auto_update_worker,
    within_auto_update_window,
)


ACTIVE_SNAPSHOT_STRATEGIES = tuple(config.SNAPSHOT_STRATEGIES)

_VALIDATION_AUTO_WORKERS = set()
_VALIDATION_AUTO_WORKERS_LOCK = threading.Lock()

# Compatibility aliases kept for existing tests and local imports.
_attach_alphalite_factors = attach_alphalite_factors
_apply_tomorrow_validation_gate = apply_tomorrow_validation_gate


def create_app() -> Flask:
    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.jinja_env.auto_reload = True
    provider = MarketDataProvider()
    quotes_cache = TimedCache(config.REFRESH_SECONDS)
    hot_cache = TimedCache(config.REFRESH_SECONDS * 2)
    industry_cache = TimedCache(config.REFRESH_SECONDS * 5)
    market_news_cache = TimedCache(config.REFRESH_SECONDS * 3)
    market_sentiment_cache = TimedCache(config.REFRESH_SECONDS * 3)
    sentiment_cache = TimedCache(config.REFRESH_SECONDS * 5)
    factors_cache = TimedCache(config.REFRESH_SECONDS * 30)
    recommendations_lock = threading.Lock()
    recommendation_cache_lock = threading.Lock()
    recommendation_cache: Dict[tuple, Dict[str, object]] = {}
    recommendation_refreshing = set()
    horizon_cache_lock = threading.Lock()
    horizon_cache: Dict[tuple, Dict[str, object]] = {}
    horizon_refreshing = set()
    snapshot_save_running = False
    snapshot_save_payload: Dict[str, object] | None = None
    recommendation_limit = max(0, int(getattr(config, "RECOMMENDATION_DISPLAY_LIMIT", 18)))
    stability_tracker = TopKDropoutTracker(
        config.STATE_PATH,
        keep_k=max(config.DEFAULT_TOP_N, recommendation_limit),
        buffer_k=max(config.DEFAULT_TOP_N * 2, recommendation_limit * 2),
    )
    validation_store = StrategyValidationStore(config.VALIDATION_DB_PATH)

    # 验证指标按 (strategy, days) 缓存：每次 /api/recommendations 刷新会触发多次
    # validation_store.metrics() 的 sqlite JOIN，验证数据通常随后台自动保存/回填更新，
    # 故在刷新周期内复用结果即可消除热路径上的重复查询。
    _metrics_cache: Dict[tuple, tuple] = {}
    _validation_summary_cache: Dict[tuple, tuple] = {}

    def cached_metrics(strategy_name: str, days: int):
        import time

        key = (strategy_name, days)
        hit = _metrics_cache.get(key)
        now = time.time()
        if hit is not None and now < hit[1]:
            return hit[0]
        value = validation_store.metrics(strategy_name, days=days)
        _metrics_cache[key] = (value, now + config.REFRESH_SECONDS)
        return value

    def invalidate_metrics_cache():
        _metrics_cache.clear()
        _validation_summary_cache.clear()

    def cached_strategy_validation_summary(strategy_name: str, days: int):
        import time

        key = (strategy_name, days)
        hit = _validation_summary_cache.get(key)
        now = time.time()
        if hit is not None and now < hit[1]:
            return hit[0]
        metrics = cached_metrics(strategy_name, days)
        deepseek_attribution_by_strategy = {
            item: validation_store.deepseek_attribution(item, days=days)
            for item in config.SNAPSHOT_STRATEGIES
        }
        latest_tuning = validation_store.latest_tuning_run(strategy_name)
        saved_deepseek_review = (latest_tuning.get("deepseek") or {}) if latest_tuning else {}
        value = {
            "metrics": metrics,
            "deepseek_attribution": deepseek_attribution_by_strategy.get(strategy_name, {}),
            "deepseek_attribution_by_strategy": deepseek_attribution_by_strategy,
            "deepseek_market_gate": validation_store.market_gate_metrics(days=days),
            "deepseek_review": saved_deepseek_review
            or {
                "enabled": False,
                "status": "not_requested",
                "strategy": strategy_name,
                "reason": "DeepSeek validation review only runs from tuning POST or scheduled end-of-day jobs.",
            },
        }
        _validation_summary_cache[key] = (value, now + min(float(config.REFRESH_SECONDS), 30.0))
        return value

    def _iteration_path() -> str:
        return getattr(config, "TOMORROW_ITERATION_PATH", ".runtime/tomorrow_iteration.json")

    def _iteration_can_apply(result: Dict[str, object]) -> bool:
        return bool(result.get("ok")) and result.get("status") == "dry_run_improved"

    def _iteration_reason(result: Dict[str, object]) -> str:
        status = str(result.get("status") or "")
        mode = str(result.get("objective_mode") or "default")
        mode_text = "方向先行" if mode == "direction_focused" else "平衡口径"
        if status == "dry_run_improved":
            return f"样本外验证改善（{mode_text}），允许人工应用。"
        if status == "insufficient_samples":
            return "有效样本不足，暂不允许自动修正。"
        if status == "insufficient_factor_coverage":
            return "因子覆盖不足，暂不允许自动修正。"
        if status == "no_oos_improvement":
            return f"样本外没有稳定改善（{mode_text}），保持当前权重。"
        if status == "insufficient_oos_folds":
            return "样本外折数不足，继续积累样本。"
        if status == "written":
            return "建议权重已写入并生效。"
        if status == "dry_run":
            return "当前权重未找到更优替代。"
        return result.get("error") or "暂无可应用建议。"

    def _current_tomorrow_weights() -> Dict[str, float]:
        from .calibrate import _current_strategy_weights

        return _current_strategy_weights("tomorrow_picks")

    def _iteration_payload(result: Dict[str, object], applied: bool = False, days: int = 120) -> Dict[str, object]:
        payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "strategy": "tomorrow_picks",
            "days": int(days),
            "objective_mode": result.get("objective_mode") or (
                "direction_focused" if getattr(config, "CALIBRATE_TOMORROW_DIRECTION_FOCUSED", False) else "default"
            ),
            "current_weights": (result.get("weights") or {}) if applied else _current_tomorrow_weights(),
            "suggested_weights": result.get("weights") or {},
            "can_apply": _iteration_can_apply(result),
            "applied": applied,
            "reason": _iteration_reason(result),
            "result": result,
        }
        return payload

    def _save_iteration_payload(payload: Dict[str, object]) -> None:
        path = _iteration_path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)

    def _load_iteration_payload() -> Dict[str, object]:
        try:
            with open(_iteration_path(), "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    def _refresh_scoring_weights(weights: Dict[str, object]) -> None:
        if not weights:
            return
        from . import scoring as scoring_module

        scoring_module.WEIGHTS.setdefault("tomorrow_picks", {}).update(weights)

    def _research_disclaimer() -> str:
        return "仅供研究，不构成投资建议。"

    def _json_response(
        *,
        ok: bool,
        status: int = 200,
        include_health: bool = True,
        include_disclaimer: bool = False,
        **payload,
    ):
        body = response_payload(
            provider.health,
            _research_disclaimer,
            ok=ok,
            include_health=include_health,
            include_disclaimer=include_disclaimer,
            **payload,
        )
        if status == 200:
            return jsonify(body)
        return jsonify(body), status

    def _error_response(error, status: int = 502, include_disclaimer: bool = False, **payload):
        return _json_response(
            ok=False,
            status=status,
            error=str(error),
            include_disclaimer=include_disclaimer,
            **payload,
        )

    def _bad_request_response(message: str, **payload):
        return _json_response(ok=False, status=400, include_health=False, error=message, **payload)

    def _attach_event_risk_layer(candidates: pd.DataFrame) -> pd.DataFrame:
        payload = load_event_risk(provider)
        candidates = attach_event_risk(candidates, payload)
        candidates = attach_risk_blacklist(candidates, load_risk_blacklist())
        codes = candidates["code"].tolist() if candidates is not None and "code" in candidates.columns else []
        return attach_fundamental_factors(candidates, load_fundamentals(provider, codes=codes))

    def _cached_hot_ranks() -> Dict[str, int]:
        hot_ranks = hot_cache.get()
        if hot_ranks is not None:
            return hot_ranks
        if config.ENABLE_HOT_RANKS:
            try:
                hot_ranks = provider.get_hot_ranks()
            except Exception:
                hot_ranks = {}
        else:
            hot_ranks = {}
        hot_cache.set(hot_ranks)
        return hot_ranks

    def _cached_industry_strength() -> Dict[str, float]:
        industry_strength = industry_cache.get()
        if industry_strength is not None:
            return industry_strength
        if config.ENABLE_INDUSTRY_STRENGTH:
            try:
                industry_strength = provider.get_industry_strength()
            except Exception:
                industry_strength = {}
        else:
            industry_strength = {}
        industry_cache.set(industry_strength)
        return industry_strength

    def _current_quotes() -> pd.DataFrame:
        quotes = quotes_cache.get()
        if quotes is None:
            quotes = provider.get_realtime_quotes()
            quotes_cache.set(quotes)
        return quotes

    def _current_quotes_or_empty() -> Tuple[pd.DataFrame, str]:
        quotes = quotes_cache.get()
        if quotes is not None:
            return quotes, ""
        try:
            quotes = provider.get_realtime_quotes()
            quotes_cache.set(quotes)
            return quotes, ""
        except Exception as exc:
            return pd.DataFrame(), str(exc)

    def _candidates_with_regime_from_quotes(
        quotes: pd.DataFrame,
        attach_codes=None,
    ) -> Tuple[pd.DataFrame, Dict[str, object]]:
        candidates = _attach_event_risk_layer(prepare_candidates(quotes))
        if attach_codes:
            candidates = attach_alphalite_factors_for_codes(provider, candidates, attach_codes)
        else:
            candidates = attach_alphalite_factors(provider, factors_cache, candidates)
        market_regime = build_market_regime(candidates, breadth_source=quotes)
        return candidates, market_regime

    def _recommendation_input_context() -> Dict[str, object]:
        quotes = _current_quotes()
        hard_filter_report = candidate_filter_report(quotes)
        candidates, market_regime = _candidates_with_regime_from_quotes(quotes)
        candidate_subset = candidates.sort_values("pct_chg", ascending=False).head(80)
        sentiment_lookup = sentiment_for_candidates(
            provider,
            sentiment_cache,
            candidate_subset[["code", "name"]].to_dict("records"),
        )
        return {
            "quotes": quotes,
            "hard_filter_report": hard_filter_report,
            "candidates": candidates,
            "market_regime": market_regime,
            "hot_ranks": _cached_hot_ranks(),
            "industry_strength": _cached_industry_strength(),
            "sentiment_lookup": sentiment_lookup,
        }

    def _cached_market_sentiment() -> Dict[str, object]:
        cached = market_sentiment_cache.get()
        if cached is not None:
            return cached
        market_news = fetch_market_news(provider, market_news_cache)
        sentiment = build_market_sentiment_index(market_news)
        market_sentiment_cache.set(sentiment)
        return sentiment

    def _stability_update_locked(horizon: str, rows: List[Dict[str, object]]) -> Dict[str, object]:
        with recommendations_lock:
            return stability_tracker.update(horizon, rows)

    def _snapshot_save_worker() -> None:
        nonlocal snapshot_save_running, snapshot_save_payload
        while True:
            with recommendation_cache_lock:
                payload = snapshot_save_payload
                snapshot_save_payload = None
                if payload is None:
                    snapshot_save_running = False
                    return
            try:
                save_recommendation_snapshot(config.RECOMMENDATION_SNAPSHOT_PATH, payload)
            except Exception:
                continue

    def _schedule_snapshot_save(payload: Dict[str, object]) -> None:
        nonlocal snapshot_save_running, snapshot_save_payload
        with recommendation_cache_lock:
            snapshot_save_payload = payload
            if snapshot_save_running:
                return
            snapshot_save_running = True
        worker = threading.Thread(
            target=_snapshot_save_worker,
            name="recommendation-snapshot-save",
            daemon=True,
        )
        worker.start()

    def _recommendation_cache_key(top_n: int, market: str) -> tuple:
        return int(top_n), str(market)

    def _horizon_cache_key(strategy: str, top_n: int, market: str) -> tuple:
        return str(strategy), int(top_n), str(market)

    def _remember_recommendation_payload(
        top_n: int,
        market: str,
        payload: Dict[str, object],
        *,
        source: str,
        stage: str = "ready",
        saved_at: str = "",
        saved_at_ts: float | None = None,
    ) -> Dict[str, object]:
        snapshot_meta = {
            "source": source,
            "stage": stage,
            "saved_at": saved_at or datetime.now().isoformat(timespec="seconds"),
            "saved_at_ts": float(saved_at_ts if saved_at_ts is not None else time.time()),
        }
        cached = {"payload": payload, "snapshot": snapshot_meta}
        with recommendation_cache_lock:
            recommendation_cache[_recommendation_cache_key(top_n, market)] = cached
        return cached

    def _cached_recommendation_entry(top_n: int, market: str) -> Dict[str, object] | None:
        with recommendation_cache_lock:
            entry = recommendation_cache.get(_recommendation_cache_key(top_n, market))
        return dict(entry) if entry else None

    def _remember_horizon_payload(
        strategy: str,
        top_n: int,
        market: str,
        payload: Dict[str, object],
        *,
        saved_at: str = "",
        saved_at_ts: float | None = None,
        source: str = "live",
    ) -> Dict[str, object]:
        cached = {
            "payload": payload,
            "saved_at": saved_at or datetime.now().isoformat(timespec="seconds"),
            "saved_at_ts": float(saved_at_ts if saved_at_ts is not None else time.time()),
            "source": source,
        }
        with horizon_cache_lock:
            horizon_cache[_horizon_cache_key(strategy, top_n, market)] = cached
        return cached

    def _cached_horizon_entry(strategy: str, top_n: int, market: str) -> Dict[str, object] | None:
        with horizon_cache_lock:
            entry = horizon_cache.get(_horizon_cache_key(strategy, top_n, market))
        return dict(entry) if entry else None

    def _snapshot_entry(top_n: int, market: str) -> Dict[str, object] | None:
        snapshot = load_recommendation_snapshot(
            config.RECOMMENDATION_SNAPSHOT_PATH,
            max_age_seconds=0,
            expected_market=market,
            expected_top_n=top_n,
        )
        if not snapshot.get("ok"):
            return None
        return _remember_recommendation_payload(
            top_n,
            market,
            dict(snapshot.get("payload") or {}),
            source="disk_snapshot",
            stage="ready",
            saved_at=str(snapshot.get("saved_at") or ""),
            saved_at_ts=time.time() - float(snapshot.get("age_seconds") or 0.0),
        )

    def _live_candidates_with_regime(attach_codes=None) -> Tuple[pd.DataFrame, pd.DataFrame, Dict[str, object]]:
        quotes = _current_quotes()
        candidates, market_regime = _candidates_with_regime_from_quotes(quotes, attach_codes=attach_codes)
        return quotes, candidates, market_regime

    def _safe_provider_health() -> Dict[str, object]:
        try:
            health = provider.health()
        except Exception as exc:
            return {"quotes_source": "unavailable", "errors": ["provider_health_failed: {}".format(exc)]}
        return dict(health) if isinstance(health, dict) else {"status": str(health)}

    def _provider_health_with_factor_coverage(
        candidates: pd.DataFrame = None,
        coverage: Dict[str, object] = None,
    ) -> Dict[str, object]:
        health = _safe_provider_health()
        if coverage is None and candidates is not None:
            try:
                coverage = factor_coverage(candidates)
            except Exception:
                coverage = None
        if coverage is not None:
            health["factor_coverage"] = coverage
            coverage_alerts = coverage.get("alerts") if isinstance(coverage, dict) else []
            if coverage_alerts:
                alerts = list(health.get("alerts") or [])
                alerts.extend(coverage_alerts)
                health["alerts"] = alerts
        return health

    def _build_horizon_payload(strategy: str, top_n: int, market: str) -> Dict[str, object]:
        _, candidates, market_regime = _live_candidates_with_regime()
        coverage = factor_coverage(candidates)
        rows, meta, _ = scored_strategy_rows(
            strategy,
            candidates,
            top_n=top_n,
            market=market,
            market_regime=market_regime,
        )
        if strategy == "tomorrow_picks":
            metrics = cached_metrics("tomorrow_picks", 20)
            apply_tomorrow_validation_gate(rows, meta, metrics)
            attach_validation_summary(rows, validation_store, "tomorrow_picks", metrics_fn=cached_metrics)
        else:
            attach_validation_summary(rows, validation_store, "swing_picks", metrics_fn=cached_metrics)
        meta["market_regime"] = market_regime
        meta["factor_coverage"] = coverage
        payload = response_payload(
            lambda: _provider_health_with_factor_coverage(coverage=coverage),
            _research_disclaimer,
            ok=True,
            include_disclaimer=True,
            data=rows,
            meta=meta,
        )
        return _remember_horizon_payload(strategy, top_n, market, payload)["payload"]

    def _refresh_validation_rows_if_needed(
        rows: List[Dict[str, object]],
        *,
        signal_date: str,
        strategy_name: str,
        should_update: bool,
    ) -> Tuple[List[Dict[str, object]], object]:
        update_result = None
        needs_update = rows and any(
            not row.get("outcome_updated_at") and not row.get("skip_reason")
            for row in rows
        )
        if should_update and needs_update:
            update_result = validation_store.update_outcomes(
                provider,
                signal_date=signal_date,
                strategy_name=strategy_name,
            )
            invalidate_metrics_cache()
            rows = validation_store.signals_for_date(signal_date, strategy_name)
        return rows, update_result

    def _attach_validation_daily_quotes(rows: List[Dict[str, object]], include_quotes: bool) -> None:
        latest_quotes_by_code = {}
        if include_quotes:
            try:
                latest_quotes_by_code = quote_lookup(_current_quotes())
            except Exception:
                latest_quotes_by_code = {}
        for row in rows:
            code = normalize_code(row.get("code", ""))
            quote = latest_quotes_by_code.get(code, {})
            current_price = coerce_number(quote.get("price"), None) if quote else None
            current_pct_chg = coerce_number(quote.get("pct_chg"), None) if quote else None
            signal_price = coerce_number(row.get("price_at_signal"), None)
            anchor_to_now_return = None
            if (
                current_price is not None
                and signal_price is not None
                and current_price > 0
                and signal_price > 0
            ):
                anchor_to_now_return = round((current_price / signal_price - 1) * 100, 4)
            row["current_price"] = current_price
            row["current_pct_chg"] = current_pct_chg
            row["anchor_to_now_return"] = anchor_to_now_return

    auto_update_lock = threading.Lock()
    auto_update_status = {
        "enabled": bool(config.VALIDATION_AUTO_UPDATE_ENABLED),
        "started": False,
        "running": False,
        "last_started_at": "",
        "last_finished_at": "",
        "last_error": "",
        "last_result": {},
        "next_run_after_seconds": config.VALIDATION_AUTO_UPDATE_INITIAL_DELAY_SECONDS,
        "next_run_at": "",
    }

    def _configured_auto_snapshot_strategies() -> List[str]:
        return configured_auto_snapshot_strategies(ACTIVE_SNAPSHOT_STRATEGIES, SNAPSHOT_STRATEGIES)

    def _validation_strategy(default: str = "short_term") -> str:
        strategy = storage_strategy_name(request.args.get("strategy", default))
        if strategy not in SNAPSHOT_STRATEGIES:
            strategy = default if default in SNAPSHOT_STRATEGIES else "short_term"
        return strategy

    def _code_batches(codes: List[str], batch_size: int) -> List[List[str]]:
        size = max(1, int(batch_size))
        return [codes[index:index + size] for index in range(0, len(codes), size)]

    def _set_auto_update_status(**values):
        set_status(auto_update_lock, auto_update_status, **values)

    auto_snapshot_lock = threading.Lock()
    auto_snapshot_status = {
        "enabled": bool(config.VALIDATION_AUTO_SNAPSHOT_ENABLED),
        "started": False,
        "running": False,
        "schedule_time": config.VALIDATION_AUTO_SNAPSHOT_TIME,
        "market": config.VALIDATION_AUTO_SNAPSHOT_MARKET,
        "last_attempt_date": "",
        "last_started_at": "",
        "last_finished_at": "",
        "last_error": "",
        "last_result": {},
        "last_tuning_date": "",
        "last_tuning_result": {},
        "next_run_at": "",
    }

    def _set_auto_snapshot_status(**values):
        set_status(auto_snapshot_lock, auto_snapshot_status, **values)

    def run_validation_tuning_once(strategies: List[str], days: int = 20, use_deepseek: bool = True) -> Dict[str, object]:
        return run_validation_tuning_once_support(
            validation_store,
            cached_metrics,
            lambda strategy, metrics, review_days: deepseek_validation_review(
                validation_store,
                strategy,
                metrics,
                review_days,
            ),
            strategies,
            days=days,
            use_deepseek=use_deepseek,
        )

    def run_validation_auto_snapshot_once() -> Dict[str, object]:
        return run_validation_auto_snapshot_once_support(
            normalize_market=_normalize_market,
            provider=provider,
            validation_store=validation_store,
            auto_snapshot_lock=auto_snapshot_lock,
            auto_snapshot_status=auto_snapshot_status,
            configured_auto_snapshot_strategies_fn=_configured_auto_snapshot_strategies,
            run_snapshots_fn=run_snapshots,
            invalidate_metrics_cache=invalidate_metrics_cache,
            run_validation_tuning_once_fn=run_validation_tuning_once,
            set_auto_snapshot_status=_set_auto_snapshot_status,
        )

    def run_validation_auto_update_once() -> Dict[str, object]:
        return run_validation_auto_update_once_support(
            auto_update_lock=auto_update_lock,
            auto_update_status=auto_update_status,
            auto_snapshot_status=auto_snapshot_status,
            set_auto_update_status=_set_auto_update_status,
            set_auto_snapshot_status=_set_auto_snapshot_status,
            run_validation_auto_snapshot_once_fn=run_validation_auto_snapshot_once,
        )

    def _normalize_market(value: str) -> str:
        text = str(value or "").strip().lower().replace(" ", "")
        if text in ("all", "main", "chinext", "star"):
            return text
        return "all"

    start_validation_auto_update_worker(
        worker_set=_VALIDATION_AUTO_WORKERS,
        worker_lock=_VALIDATION_AUTO_WORKERS_LOCK,
        set_auto_update_status=_set_auto_update_status,
        within_auto_update_window_fn=lambda now: within_auto_update_window(
            now,
            getattr(config, "VALIDATION_AUTO_UPDATE_START_TIME", "14:30"),
            getattr(config, "VALIDATION_AUTO_UPDATE_UNTIL_TIME", "23:59"),
        ),
        next_auto_update_window_start_fn=lambda now: next_auto_update_window_start(
            now,
            getattr(config, "VALIDATION_AUTO_UPDATE_START_TIME", "14:30"),
            getattr(config, "VALIDATION_AUTO_UPDATE_UNTIL_TIME", "23:59"),
        ),
        run_validation_auto_update_once_fn=run_validation_auto_update_once,
    )
    start_validation_auto_snapshot_worker(
        worker_set=_VALIDATION_AUTO_WORKERS,
        worker_lock=_VALIDATION_AUTO_WORKERS_LOCK,
        auto_snapshot_lock=auto_snapshot_lock,
        auto_snapshot_status=auto_snapshot_status,
        auto_snapshot_time_parts_fn=lambda: auto_snapshot_time_parts(config.VALIDATION_AUTO_SNAPSHOT_TIME),
        next_auto_snapshot_at_fn=lambda now: next_auto_snapshot_at(now, config.VALIDATION_AUTO_SNAPSHOT_TIME),
        set_auto_snapshot_status=_set_auto_snapshot_status,
        run_validation_auto_snapshot_once_fn=run_validation_auto_snapshot_once,
    )

    def _build_recommendations_payload(top_n: int, market: str, include_deepseek: bool = True) -> tuple:
        try:
            blacklist_payload = load_risk_blacklist()
            context = _recommendation_input_context()
            hard_filter_report = context["hard_filter_report"]
            candidates = context["candidates"]
            market_regime = context["market_regime"]
            hot_ranks = context["hot_ranks"]
            industry_strength = context["industry_strength"]
            sentiment_lookup = context["sentiment_lookup"]
            coverage = factor_coverage(candidates)

            recommendations_by_horizon, meta, deepseek_meta_by_strategy = build_recommendation_horizons(
                candidates,
                top_n,
                market,
                market_regime,
                hot_ranks,
                industry_strength,
                sentiment_lookup,
                cached_metrics,
                apply_deepseek=include_deepseek,
            )
            short_display_rows, meta = finalize_recommendation_payload_meta(
                recommendations_by_horizon["short_term"],
                meta,
                blacklist_payload,
                hard_filter_report,
                market_regime,
                deepseek_meta_by_strategy,
                top_n,
                _stability_update_locked,
                validation_store,
                cached_metrics,
            )
            meta["factor_coverage"] = coverage
            try:
                market_gate = meta.get("deepseek_market_gate") if isinstance(meta, dict) else {}
                if isinstance(market_gate, dict) and market_gate.get("enabled"):
                    validation_store.save_market_gate_review(market_gate, market_filter=market)
            except Exception:
                pass
            recommendations_by_horizon = {"short_term": short_display_rows}

            payload = {
                "ok": True,
                "data": recommendations_by_horizon["short_term"],
                "recommendations": recommendations_by_horizon,
                "meta": meta,
                "market_sentiment": _cached_market_sentiment(),
                "health": _provider_health_with_factor_coverage(coverage=coverage),
                "disclaimer": _research_disclaimer(),
            }
            _schedule_snapshot_save(payload)
            _remember_recommendation_payload(
                top_n,
                market,
                payload,
                source="live",
                stage="ready" if include_deepseek else "local_only",
            )
            return payload, 200
        except Exception as exc:
            snapshot = load_recommendation_snapshot(
                config.RECOMMENDATION_SNAPSHOT_PATH,
                max_age_seconds=getattr(config, "RECOMMENDATION_SNAPSHOT_MAX_AGE_SECONDS", 300),
                expected_market=market,
                expected_top_n=top_n,
            )
            if snapshot.get("ok"):
                return snapshot_fallback_payload(snapshot, exc), 200
            return error_payload(provider.health, _research_disclaimer, exc), 502

    def _recommendation_snapshot_info(entry: Dict[str, object]) -> Dict[str, object]:
        snapshot = dict(entry.get("snapshot") or {})
        saved_at_ts = float(snapshot.get("saved_at_ts") or 0.0)
        snapshot["age_seconds"] = round(max(0.0, time.time() - saved_at_ts), 2) if saved_at_ts else None
        return snapshot

    def _serve_recommendation_payload(entry: Dict[str, object]) -> Dict[str, object]:
        payload = dict(entry.get("payload") or {})
        payload["snapshot"] = _recommendation_snapshot_info(entry)
        return payload

    def _refresh_recommendation_cache(top_n: int, market: str) -> None:
        key = _recommendation_cache_key(top_n, market)
        try:
            _build_recommendations_payload(top_n, market, include_deepseek=True)
        finally:
            with recommendation_cache_lock:
                recommendation_refreshing.discard(key)

    def _schedule_recommendation_refresh(top_n: int, market: str) -> bool:
        key = _recommendation_cache_key(top_n, market)
        with recommendation_cache_lock:
            if key in recommendation_refreshing:
                return False
            recommendation_refreshing.add(key)
        worker = threading.Thread(
            target=_refresh_recommendation_cache,
            args=(top_n, market),
            name=f"recommendation-refresh-{market}-{top_n}",
            daemon=True,
        )
        worker.start()
        return True

    def _recommendations_payload(top_n: int, market: str) -> tuple:
        refresh_after_seconds = max(5, int(config.REFRESH_SECONDS))
        entry = _cached_recommendation_entry(top_n, market) or _snapshot_entry(top_n, market)
        if entry is not None:
            snapshot = _recommendation_snapshot_info(entry)
            age_seconds = float(snapshot.get("age_seconds") or 0.0)
            if age_seconds >= refresh_after_seconds or snapshot.get("stage") != "ready":
                _schedule_recommendation_refresh(top_n, market)
            return _serve_recommendation_payload(entry), 200
        payload, status = _build_recommendations_payload(top_n, market, include_deepseek=False)
        if status == 200:
            _schedule_recommendation_refresh(top_n, market)
            entry = _cached_recommendation_entry(top_n, market)
            if entry is not None:
                return _serve_recommendation_payload(entry), status
        return payload, status

    def _refresh_horizon_cache(strategy: str, top_n: int, market: str) -> None:
        key = _horizon_cache_key(strategy, top_n, market)
        try:
            _build_horizon_payload(strategy, top_n, market)
        except Exception as exc:
            payload = response_payload(
                _safe_provider_health,
                _research_disclaimer,
                ok=False,
                include_disclaimer=True,
                error=str(exc),
                data=[],
                meta={
                    "generated_at": datetime.now().isoformat(timespec="seconds"),
                    "candidate_count": 0,
                    "display_count": 0,
                    "display_limit": top_n,
                    "top_n": top_n,
                    "market_filter": market,
                    "strategy_label": "明天推荐" if strategy == "tomorrow_picks" else "2-5天推荐",
                    "strategy": "实时行情刷新失败",
                    "fallback": "live_refresh_failed",
                },
            )
            _remember_horizon_payload(strategy, top_n, market, payload, source="live_refresh_failed")
        finally:
            with horizon_cache_lock:
                horizon_refreshing.discard(key)

    def _schedule_horizon_refresh(strategy: str, top_n: int, market: str) -> bool:
        key = _horizon_cache_key(strategy, top_n, market)
        with horizon_cache_lock:
            if key in horizon_refreshing:
                return False
            horizon_refreshing.add(key)
        worker = threading.Thread(
            target=_refresh_horizon_cache,
            args=(strategy, top_n, market),
            name=f"horizon-refresh-{strategy}-{market}-{top_n}",
            daemon=True,
        )
        worker.start()
        return True

    def _saved_horizon_payload(strategy: str, top_n: int, market: str) -> Dict[str, object] | None:
        saved_rows = validation_store.latest_signal_rows(strategy)
        if not saved_rows:
            return None
        if strategy == "tomorrow_picks":
            return saved_tomorrow_fallback_payload(
                saved_rows=saved_rows,
                top_n=top_n,
                market=market,
                detailed=True,
                validation_store=validation_store,
                cached_metrics_fn=cached_metrics,
                load_risk_blacklist_fn=load_risk_blacklist,
                analysis_window_fn=lambda: analysis_window(config.VALIDATION_AUTO_SNAPSHOT_TIME),
                provider_health_fn=provider.health,
                research_disclaimer_fn=_research_disclaimer,
            )
        attach_validation_summary(saved_rows, validation_store, strategy, metrics_fn=cached_metrics)
        return response_payload(
            provider.health,
            _research_disclaimer,
            ok=True,
            include_disclaimer=True,
            data=saved_rows[:top_n],
            meta={
                "generated_at": "",
                "candidate_count": len(saved_rows),
                "screened_count": len(saved_rows),
                "display_count": min(len(saved_rows), top_n),
                "display_limit": top_n,
                "top_n": top_n,
                "market_filter": market,
                "strategy_version": "swing_picks_v1",
                "strategy_label": "2-5天推荐",
                "strategy": "后台刷新中，先显示最近保存的 2-5 天推荐",
                "fallback": "saved_snapshot",
            },
        )

    def _horizon_payload(strategy: str, top_n: int, market: str) -> tuple:
        refresh_after_seconds = max(5, int(config.REFRESH_SECONDS))
        entry = _cached_horizon_entry(strategy, top_n, market)
        if entry is not None:
            age_seconds = max(0.0, time.time() - float(entry.get("saved_at_ts") or 0.0))
            payload = dict(entry.get("payload") or {})
            payload["snapshot"] = {
                "saved_at": entry.get("saved_at", ""),
                "age_seconds": round(age_seconds, 2),
                "source": entry.get("source", "memory_cache"),
            }
            if age_seconds >= refresh_after_seconds:
                _schedule_horizon_refresh(strategy, top_n, market)
            return payload, 200
        _schedule_horizon_refresh(strategy, top_n, market)
        payload = _saved_horizon_payload(strategy, top_n, market)
        if payload is not None:
            payload["snapshot"] = {
                "saved_at": "",
                "age_seconds": None,
                "source": "saved_snapshot",
            }
            return payload, 200
        return response_payload(
            provider.health,
            _research_disclaimer,
            ok=True,
            include_disclaimer=True,
            data=[],
            meta={
                "generated_at": "",
                "candidate_count": 0,
                "display_count": 0,
                "display_limit": top_n,
                "top_n": top_n,
                "market_filter": market,
                "strategy_label": "明天推荐" if strategy == "tomorrow_picks" else "2-5天推荐",
                "strategy": "后台刷新中",
                "fallback": "async_refresh_pending",
            },
            snapshot={"saved_at": "", "age_seconds": None, "source": "async_refresh_pending"},
        ), 200

    def _sse_event(event: str, payload: Dict[str, object]) -> str:
        return "event: {}\ndata: {}\n\n".format(
            event,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )

    @app.route("/")
    def index():
        return render_template(
            "index.html",
            refresh_seconds=config.REFRESH_SECONDS,
            default_top_n=config.DEFAULT_TOP_N,
            recommendation_snapshot_max_age_seconds=getattr(config, "RECOMMENDATION_SNAPSHOT_MAX_AGE_SECONDS", 300),
        )

    @app.route("/api/recommendations")
    def recommendations():
        top_n = _int_arg("top_n", config.DEFAULT_TOP_N, minimum=0, maximum=config.RECOMMENDATION_MAX_TOP_N)
        market = _normalize_market(request.args.get("market", "all"))
        payload, status = _recommendations_payload(top_n, market)
        return (jsonify(payload), status) if status != 200 else jsonify(payload)

    @app.route("/api/recommendations/latest")
    def latest_recommendations():
        top_n = _int_arg("top_n", config.DEFAULT_TOP_N, minimum=0, maximum=config.RECOMMENDATION_MAX_TOP_N)
        market = _normalize_market(request.args.get("market", "all"))
        max_age = _int_arg(
            "max_age",
            getattr(config, "RECOMMENDATION_SNAPSHOT_MAX_AGE_SECONDS", 300),
            minimum=0,
            maximum=86400,
        )
        snapshot = load_recommendation_snapshot(
            config.RECOMMENDATION_SNAPSHOT_PATH,
            max_age_seconds=max_age,
            expected_market=market,
            expected_top_n=top_n,
        )
        if snapshot.get("ok"):
            payload = dict(snapshot["payload"])
            payload["snapshot"] = {
                "saved_at": snapshot.get("saved_at", ""),
                "age_seconds": snapshot.get("age_seconds"),
                "path": snapshot.get("path"),
            }
            return jsonify(payload)
        return _json_response(ok=False, status=404, snapshot=snapshot)

    @app.route("/api/recommendations/stream")
    def recommendations_stream():
        top_n = _int_arg("top_n", config.DEFAULT_TOP_N, minimum=0, maximum=config.RECOMMENDATION_MAX_TOP_N)
        market = _normalize_market(request.args.get("market", "all"))
        refresh_seconds = max(5, int(config.REFRESH_SECONDS))

        @stream_with_context
        def generate():
            yield "retry: 5000\n\n"
            while True:
                payload, status = _recommendations_payload(top_n, market)
                yield _sse_event("recommendations" if status == 200 and payload.get("ok") else "recommendations-error", payload)
                time.sleep(refresh_seconds)

        return Response(
            generate(),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.route("/api/health")
    def health():
        coverage = {
            "row_count": 0,
            "avg_data_coverage": 0.0,
            "alphalite_ready_ratio": 0.0,
            "alphalite_not_ready_ratio": 1.0,
            "alphalite_zero_coverage_ratio": 1.0,
            "columns": {},
            "degraded": True,
            "alerts": [],
        }
        blacklist_payload = load_risk_blacklist()
        try:
            quotes = _current_quotes()
            candidates, _ = _candidates_with_regime_from_quotes(quotes)
            coverage = factor_coverage(candidates)
        except Exception:
            pass
        provider_health = _provider_health_with_factor_coverage(coverage=coverage)
        return jsonify(
            {
                "ok": True,
                "refresh_seconds": config.REFRESH_SECONDS,
                "supported_markets": config.MARKET_LABELS,
                "factor_coverage": coverage,
                "event_risk": {
                    "enabled": bool(config.ENABLE_EVENT_RISK),
                    "status": load_event_risk(provider).get("status", "disabled"),
                },
                "risk_blacklist": risk_blacklist_summary(blacklist_payload),
                "factor_ic": {
                    "enabled": bool(config.ENABLE_FUNDAMENTALS),
                    "fundamentals_status": load_fundamentals(provider).get("status", "disabled"),
                    "generated_at": load_factor_ic().get("generated_at", ""),
                },
                "health": provider_health,
            }
        )

    @app.route("/api/stock-prediction/<code>")
    def stock_prediction(code: str):
        normalized_code = code.strip()[:12]
        try:
            quotes, quote_error = _current_quotes_or_empty()
            candidates, market_regime = _candidates_with_regime_from_quotes(
                quotes,
                attach_codes=[normalized_code],
            )
            top_n = max(1, len(candidates))
            prediction_sentiment_lookup = {}
            if (
                candidates is not None
                and not candidates.empty
                and "pct_chg" in candidates.columns
                and "code" in candidates.columns
                and "name" in candidates.columns
            ):
                candidate_subset = candidates.sort_values("pct_chg", ascending=False).head(80)
                prediction_sentiment_lookup = sentiment_for_candidates(
                    provider,
                    sentiment_cache,
                    candidate_subset[["code", "name"]].to_dict("records"),
                )
            short_term_snapshot_rows = None
            short_term_snapshot_meta = None
            snapshot = load_recommendation_snapshot(
                config.RECOMMENDATION_SNAPSHOT_PATH,
                max_age_seconds=getattr(config, "RECOMMENDATION_SNAPSHOT_MAX_AGE_SECONDS", 300),
                expected_market="all",
                expected_top_n=config.DEFAULT_TOP_N,
            )
            if snapshot.get("ok"):
                snapshot_payload = snapshot.get("payload") or {}
                short_term_snapshot_rows = (
                    (snapshot_payload.get("recommendations") or {}).get("short_term")
                    or snapshot_payload.get("data")
                    or []
                )
                short_term_snapshot_meta = {
                    "strategy_version": str((snapshot_payload.get("meta") or {}).get("strategy_version", "")),
                    "missed_reason": "未进入当前推荐展示榜，可能因排序靠后、主题限流或稳定性裁剪",
                }
            prediction_rows, strategy_metas = prediction_strategy_rows(
                candidates,
                top_n=top_n,
                market_regime=market_regime,
                hot_ranks=_cached_hot_ranks(),
                industry_strength=_cached_industry_strength(),
                sentiment_lookup=prediction_sentiment_lookup,
                short_term_rows_override=short_term_snapshot_rows,
                short_term_meta_override=short_term_snapshot_meta,
            )
            fallback_history = None
            fallback_error = ""
            normalized_for_lookup = normalize_code(normalized_code)
            if not stock_exists_in_quotes(normalized_for_lookup, quotes):
                try:
                    fallback_history = provider.get_history(normalized_for_lookup, days=120)
                except Exception as exc:
                    fallback_error = str(exc)
            if quote_error:
                fallback_error = "; ".join(item for item in (quote_error, fallback_error) if item)
            result = build_stock_prediction(
                normalized_code,
                candidates,
                prediction_rows,
                strategy_metas=strategy_metas,
                market_regime=market_regime,
                raw_quotes=quotes,
                fallback_history=fallback_history,
                fallback_error=fallback_error,
            )
            deepseek_requested = request.args.get("deepseek", "0").lower() in ("1", "true", "yes", "on")
            if deepseek_requested and bool(result.get("ok")):
                result["optimization"] = deepseek_stock_prediction_review(result)
            if bool(getattr(config, "ENABLE_STANCE_TRACKING", False)) and bool(result.get("ok")):
                try:
                    result["stance_tracking"] = validation_store.save_stock_prediction_snapshot(result)
                except Exception as exc:
                    result["stance_tracking"] = {"saved": 0, "status": "error", "error": str(exc)}
            result_ok = bool(result.get("ok"))
            result_payload = dict(result)
            result_payload.pop("ok", None)
            return _json_response(ok=result_ok, **result_payload)
        except Exception as exc:
            return _error_response(exc)

    @app.route("/api/stock-prediction/stance-validation")
    def stock_prediction_stance_validation():
        days = _int_arg("days", 120, minimum=1, maximum=500)
        try:
            return _json_response(ok=True, enabled=bool(getattr(config, "ENABLE_STANCE_TRACKING", False)), metrics=validation_store.stance_metrics(days=days))
        except Exception as exc:
            return _error_response(exc)

    @app.route("/api/stock-prediction/stance-validation/update", methods=["POST"])
    def stock_prediction_stance_validation_update():
        days = _int_arg("days", 120, minimum=1, maximum=500)
        try:
            result = validation_store.update_stock_prediction_outcomes(provider, days=days)
            return _json_response(ok=True, enabled=bool(getattr(config, "ENABLE_STANCE_TRACKING", False)), result=result)
        except Exception as exc:
            return _error_response(exc)

    @app.route("/api/tomorrow-picks")
    def tomorrow_picks():
        top_n = _int_arg("top_n", config.TOMORROW_TOP_N, minimum=0, maximum=config.RECOMMENDATION_MAX_TOP_N)
        market = _normalize_market(request.args.get("market", "all"))
        try:
            payload, status = _horizon_payload("tomorrow_picks", top_n, market)
            return (jsonify(payload), status) if status != 200 else jsonify(payload)
        except Exception as exc:
            saved_rows = validation_store.latest_signal_rows("tomorrow_picks")
            if saved_rows:
                return jsonify(
                    saved_tomorrow_fallback_payload(
                        saved_rows=saved_rows,
                        top_n=top_n,
                        market=market,
                        detailed=True,
                        validation_store=validation_store,
                        cached_metrics_fn=cached_metrics,
                        load_risk_blacklist_fn=load_risk_blacklist,
                        analysis_window_fn=lambda: analysis_window(config.VALIDATION_AUTO_SNAPSHOT_TIME),
                        provider_health_fn=provider.health,
                        research_disclaimer_fn=_research_disclaimer,
                    )
                )
            return _error_response(exc, include_disclaimer=True)

    @app.route("/api/swing-picks")
    def swing_picks():
        top_n = _int_arg("top_n", config.DEFAULT_TOP_N, minimum=0, maximum=config.RECOMMENDATION_MAX_TOP_N)
        market = _normalize_market(request.args.get("market", "all"))
        try:
            payload, status = _horizon_payload("swing_picks", top_n, market)
            return (jsonify(payload), status) if status != 200 else jsonify(payload)
        except Exception as exc:
            return _error_response(exc, include_disclaimer=True)

    @app.route("/api/strategy-validation/snapshot", methods=["POST"])
    def strategy_snapshot():
        strategy = request.args.get("strategy", "short_term")
        market = _normalize_market(request.args.get("market", "all"))
        if strategy not in SNAPSHOT_STRATEGIES:
            strategy = "short_term"
        try:
            result = run_snapshot(provider, validation_store, strategy, market=market)
            invalidate_metrics_cache()
            result_ok = bool(result.get("ok"))
            result_payload = dict(result)
            result_payload.pop("ok", None)
            return _json_response(ok=result_ok, **result_payload)
        except Exception as exc:
            return _error_response(exc)

    @app.route("/api/strategy-validation/update", methods=["POST"])
    def strategy_validation_update():
        signal_date = request.args.get("date", "")
        strategy = _validation_strategy()
        try:
            result = validation_store.update_outcomes(
                provider,
                signal_date=signal_date,
                strategy_name=strategy,
            )
            invalidate_metrics_cache()
            return _json_response(ok=True, result=result)
        except Exception as exc:
            return _error_response(exc)

    @app.route("/api/strategy-validation/auto-update-status")
    def strategy_validation_auto_update_status():
        with auto_update_lock:
            status = dict(auto_update_status)
        with auto_snapshot_lock:
            snapshot_status = dict(auto_snapshot_status)
        status["config"] = {
            "mode": "recommendation_snapshot",
            "initial_delay_seconds": config.VALIDATION_AUTO_UPDATE_INITIAL_DELAY_SECONDS,
            "interval_seconds": config.VALIDATION_AUTO_UPDATE_INTERVAL_SECONDS,
            "strategies": _configured_auto_snapshot_strategies(),
            "start_time": getattr(config, "VALIDATION_AUTO_UPDATE_START_TIME", "14:30"),
            "until_time": getattr(config, "VALIDATION_AUTO_UPDATE_UNTIL_TIME", "23:59"),
        }
        snapshot_status["config"] = {
            "enabled": bool(config.VALIDATION_AUTO_SNAPSHOT_ENABLED),
            "time": config.VALIDATION_AUTO_SNAPSHOT_TIME,
            "retry_seconds": getattr(config, "VALIDATION_AUTO_SNAPSHOT_RETRY_SECONDS", 600),
            "market": config.VALIDATION_AUTO_SNAPSHOT_MARKET,
            "strategies": _configured_auto_snapshot_strategies(),
            "weekdays_only": True,
        }
        return _json_response(ok=True, auto_update=status, auto_snapshot=snapshot_status)

    @app.route("/api/strategy-validation/prefetch-history", methods=["POST"])
    def strategy_validation_prefetch_history():
        signal_date = request.args.get("date", "")
        strategy = _validation_strategy()
        days = _int_arg("days", 180, minimum=30, maximum=500)
        limit = _int_arg("limit", 500, minimum=1, maximum=2000)
        force = request.args.get("force", "0") in ("1", "true", "yes")
        update = request.args.get("update", "1") not in ("0", "false", "no")
        try:
            code_rows = validation_store.signal_codes(
                signal_date=signal_date,
                strategy_name=strategy,
                limit=limit,
            )
            codes = [row["code"] for row in code_rows]
            prefetch = provider.prefetch_history(codes, days=days, force=force)
            if int(prefetch.get("downloaded") or 0) > 0:
                factors_cache.clear()
            outcome = None
            if update:
                outcome = validation_store.update_outcomes(
                    provider,
                    signal_date=signal_date,
                    strategy_name=strategy,
                )
                invalidate_metrics_cache()
            return _json_response(ok=True, codes=code_rows, prefetch=prefetch, outcome=outcome)
        except Exception as exc:
            return _error_response(exc)

    @app.route("/api/strategy-validation/backfill-samples", methods=["POST"])
    def strategy_validation_backfill_samples():
        strategy = _validation_strategy()
        days = _int_arg("days", 260, minimum=80, maximum=600)
        replay_days = _int_arg("replay_days", 20, minimum=1, maximum=80)
        top_n = _int_arg("top_n", 30, minimum=1, maximum=50)
        holding_days = _int_arg("holding_days", 3, minimum=1, maximum=20)
        limit = _int_arg("limit", 120, minimum=10, maximum=500)
        force = request.args.get("force", "0") in ("1", "true", "yes")
        try:
            code_rows = validation_store.signal_codes(strategy_name=strategy, limit=limit)
            if not code_rows:
                code_rows = candidate_code_rows(provider, quotes_cache, limit)
            codes = [row["code"] for row in code_rows]
            code_names = {row["code"]: row.get("name") or row["code"] for row in code_rows}
            prefetch = provider.prefetch_history(codes, days=days, force=force)
            if int(prefetch.get("downloaded") or 0) > 0:
                factors_cache.clear()
            replay = backfill_strategy_validation_samples(
                provider,
                validation_store,
                strategy,
                codes,
                code_names=code_names,
                days=days,
                replay_days=replay_days,
                top_n=top_n,
                holding_days=holding_days,
            )
            invalidate_metrics_cache()
            metrics = validation_store.metrics(strategy, days=120)
            return _json_response(
                ok=bool(replay.get("ok")),
                codes=code_rows,
                prefetch=prefetch,
                replay=replay,
                metrics=metrics,
            )
        except Exception as exc:
            return _error_response(exc)

    @app.route("/api/strategy-validation")
    def strategy_validation():
        strategy = _validation_strategy()
        days = _int_arg("days", 20, minimum=1, maximum=120)
        try:
            dates = validation_store.list_signal_dates(strategy)
            light = request.args.get("light", "0").lower() in ("1", "true", "yes", "on")
            if light:
                return _json_response(ok=True, include_health=False, strategy=strategy, dates=dates)
            summary = cached_strategy_validation_summary(strategy, days)
            return _json_response(
                ok=True,
                strategy=strategy,
                dates=dates,
                **summary,
            )
        except Exception as exc:
            return _error_response(exc)

    @app.route("/api/strategy-validation/tuning", methods=["GET", "POST"])
    def strategy_validation_tuning():
        strategy = _validation_strategy()
        days = _int_arg("days", 20, minimum=1, maximum=120)
        try:
            if request.method == "GET":
                return _json_response(
                    ok=True,
                    include_health=False,
                    strategy=strategy,
                    latest=validation_store.latest_tuning_run(strategy),
                )
            use_deepseek = request.args.get("deepseek", "1").lower() not in ("0", "false", "no", "off")
            tuning_result = run_validation_tuning_once([strategy], days=days, use_deepseek=use_deepseek)
            invalidate_metrics_cache()
            latest = validation_store.latest_tuning_run(strategy)
            plan = latest.get("plan") or {}
            return _json_response(
                ok=bool(tuning_result.get("ok")),
                include_health=False,
                strategy=strategy,
                plan=plan,
                saved=(tuning_result.get("runs") or [{}])[0].get("saved", {}),
                latest=latest,
                tuning=tuning_result,
            )
        except Exception as exc:
            return _error_response(exc)

    @app.route("/api/strategy-validation/daily")
    def strategy_validation_daily():
        signal_date = request.args.get("date", "")
        strategy = _validation_strategy()
        if not signal_date:
            return _bad_request_response("缺少 date 参数")
        try:
            rows = validation_store.signals_for_date(signal_date, strategy)
            should_update = request.args.get("update", "0").lower() in ("1", "true", "yes", "on")
            include_quotes = request.args.get("quotes", "0").lower() in ("1", "true", "yes", "on")
            rows, update_result = _refresh_validation_rows_if_needed(
                rows,
                signal_date=signal_date,
                strategy_name=strategy,
                should_update=should_update,
            )
            _attach_validation_daily_quotes(rows, include_quotes)
            summary = validation_batch_summary(rows, strategy)
            return _json_response(ok=True, date=signal_date, data=rows, summary=summary, update=update_result)
        except Exception as exc:
            return _error_response(exc)

    @app.route("/api/tomorrow-iteration")
    def tomorrow_iteration():
        days = _int_arg("days", 120, minimum=30, maximum=240)
        force = request.args.get("force", "0") in ("1", "true", "yes", "on")
        raw_direction_focus = request.args.get("direction_focus")
        direction_focus = None
        if raw_direction_focus is not None:
            direction_focus = raw_direction_focus.lower() in ("1", "true", "yes", "on")
        try:
            cached = _load_iteration_payload()
            if cached and int(cached.get("days") or 0) == days and not force:
                return _json_response(ok=True, iteration=cached)
            from .calibrate import calibrate_live_weights

            result = calibrate_live_weights(
                "tomorrow_picks",
                db_path=config.VALIDATION_DB_PATH,
                top_k=10,
                days=days,
                steps=2,
                dry_run=True,
                direction_focus=direction_focus,
            )
            payload = _iteration_payload(result, days=days)
            _save_iteration_payload(payload)
            return _json_response(ok=True, iteration=payload)
        except Exception as exc:
            cached = _load_iteration_payload()
            return _error_response(exc, iteration=cached)

    @app.route("/api/tomorrow-iteration/apply", methods=["POST"])
    def tomorrow_iteration_apply():
        days = _int_arg("days", 120, minimum=30, maximum=240)
        raw_direction_focus = request.args.get("direction_focus")
        direction_focus = None
        if raw_direction_focus is not None:
            direction_focus = raw_direction_focus.lower() in ("1", "true", "yes", "on")
        try:
            from .calibrate import calibrate_live_weights

            dry_result = calibrate_live_weights(
                "tomorrow_picks",
                db_path=config.VALIDATION_DB_PATH,
                top_k=10,
                days=days,
                steps=2,
                dry_run=True,
                direction_focus=direction_focus,
            )
            dry_payload = _iteration_payload(dry_result, days=days)
            if not dry_payload["can_apply"]:
                _save_iteration_payload(dry_payload)
                return _json_response(
                    ok=False,
                    status=409,
                    error=dry_payload["reason"],
                    iteration=dry_payload,
                )

            written_result = calibrate_live_weights(
                "tomorrow_picks",
                db_path=config.VALIDATION_DB_PATH,
                top_k=10,
                days=days,
                steps=2,
                dry_run=False,
                direction_focus=direction_focus,
            )
            _refresh_scoring_weights(written_result.get("weights") or {})
            payload = _iteration_payload(written_result, applied=written_result.get("status") == "written", days=days)
            _save_iteration_payload(payload)
            return _json_response(ok=True, iteration=payload)
        except Exception as exc:
            return _error_response(exc, iteration=_load_iteration_payload())

    @app.route("/api/backtest")
    def backtest():
        codes = parse_code_list(request.args.get("codes", ""))
        if not codes:
            codes = list_market_data_codes(config.MARKET_DATA_DB_PATH)[:500]
        if not codes:
            try:
                quotes = _current_quotes()
                candidates, _ = _candidates_with_regime_from_quotes(quotes)
                codes = candidates.sort_values(["pct_chg", "turnover"], ascending=False).head(40)[
                    "code"
                ].tolist()
            except Exception:
                codes = parse_code_list("600000,000001,300750,688981")
        top_k = _int_arg("top_k", 10, minimum=1, maximum=30)
        holding_days = _int_arg("holding_days", 3, minimum=1, maximum=20)
        lookback_days = _int_arg("lookback_days", 30, minimum=20, maximum=120)
        rebalance_step = _int_arg("rebalance_step", 1, minimum=1, maximum=20)
        mode = request.args.get("mode", "rolling")
        history_by_code = load_local_history_frames(codes[:500], days=160)
        for code in codes[:60]:
            if code in history_by_code:
                continue
            try:
                history = provider.get_history(code, days=160)
            except Exception:
                continue
            if history is not None and not history.empty:
                history_by_code[code] = history
        if mode == "snapshot":
            result = run_alphalite_backtest(
                history_by_code,
                top_k=top_k,
                holding_days=holding_days,
            )
        else:
            result = run_rolling_alphalite_backtest(
                history_by_code,
                top_k=top_k,
                holding_days=holding_days,
                lookback_days=lookback_days,
                rebalance_step=rebalance_step,
            )
        return jsonify(result)

    return app


def _int_arg(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(request.args.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))
