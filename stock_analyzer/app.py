from datetime import datetime
import threading
import time
from typing import Dict, List

from flask import Flask, jsonify, render_template, request
import pandas as pd

from . import config
from .backtest import parse_code_list, run_alphalite_backtest, run_rolling_alphalite_backtest
from .event_risk import attach_event_risk, load_event_risk
from .factor_ic import load_factor_ic
from .factors import build_alphalite_factors, merge_alphalite
from .fundamentals import attach_fundamental_factors, load_fundamentals
from .paper_trading import PaperTradingStore
from .providers import MarketDataProvider, TimedCache
from .portfolio import build_portfolio
from .prediction import build_stock_prediction
from .normalization import coerce_number, normalize_code
from .selfcheck import factor_coverage
from .scoring import (
    CHOKEPOINT_INDUSTRY_LEADERS,
    SERENITY_REFERENCES,
    STRATEGY_LABELS,
    TRADING_AGENTS_REFERENCE,
    build_market_regime,
    build_strategy_consensus,
    prepare_candidates,
    score_position_candidates,
    score_chokepoint_candidates,
    score_reversal_candidates,
    score_smallcap_value_candidates,
    score_breakout_candidates,
    score_dual_horizon_candidates,
    score_swing_candidates,
    score_tech_potential_candidates,
    score_tomorrow_candidates,
)
from .sentiment import build_market_sentiment_index, score_stock_sentiment
from .strategy_validation import StrategyValidationStore
from .strategy_health import save_strategy_status, strategy_status
from .snapshot import SNAPSHOT_STRATEGIES, run_snapshot
from .validation_replay import backfill_strategy_validation_samples
from .stability import TopKDropoutTracker


STRATEGY_CATALOG = (
    {
        "name": "tomorrow_picks",
        "label": "明天预测",
        "version": "tomorrow_picks_v2",
        "horizon": "次日",
        "goal": "14:30 后筛选次日可能冲高且仍可买的股票",
        "route": "/api/tomorrow-picks",
    },
    {
        "name": "swing_picks",
        "label": "波段 5-10 日",
        "version": "swing_5_10d_v1",
        "horizon": "5-10日",
        "goal": "筛选短周期趋势延续、温和放量且不过热的股票",
        "route": "/api/swing-picks",
    },
    {
        "name": "position_picks",
        "label": "中长期 1-3 月",
        "version": "position_1_3m_v1",
        "horizon": "1-3月",
        "goal": "技术趋势版中长期候选，偏好趋势稳健、波动可控、涨幅未透支",
        "route": "/api/position-picks",
    },
    {
        "name": "tech_potential",
        "label": "科技潜力",
        "version": "tech_potential_v1",
        "horizon": "主题潜力",
        "goal": "匹配科技方向并过滤前期涨幅明显透支的股票",
        "route": "/api/tech-potential",
    },
    {
        "name": "chokepoint_picks",
        "label": "卡脖子",
        "version": "chokepoint_v1",
        "horizon": "供应链上游",
        "goal": "上溯供应链，挖掘供给最紧、最难替代、尚未被重定价的卡脖子环节",
        "route": "/api/chokepoint-picks",
    },
    {
        "name": "reversal_picks",
        "label": "反转低波",
        "version": "reversal_v1",
        "horizon": "1-2周",
        "goal": "A股短线反转+低波动+高换手回避，挖掘超跌且不躁动的标的",
        "route": "/api/reversal-picks",
    },
    {
        "name": "smallcap_value_picks",
        "label": "小市值价值",
        "version": "smallcap_value_v1",
        "horizon": "1-3月",
        "goal": "低流通市值+低PE/PB，含市值下限、亏损过滤、流动性与防守降权护栏",
        "route": "/api/smallcap-value-picks",
    },
    {
        "name": "breakout_picks",
        "label": "量价突破",
        "version": "breakout_v1",
        "horizon": "5-10日",
        "goal": "均线多头排列或20日新高 + 量能突破的趋势确认型选股",
        "route": "/api/breakout-picks",
    },
)

_VALIDATION_AUTO_WORKERS = set()
_VALIDATION_AUTO_WORKERS_LOCK = threading.Lock()


def create_app() -> Flask:
    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    provider = MarketDataProvider()
    quotes_cache = TimedCache(config.REFRESH_SECONDS)
    hot_cache = TimedCache(config.REFRESH_SECONDS * 2)
    industry_cache = TimedCache(config.REFRESH_SECONDS * 5)
    market_news_cache = TimedCache(config.REFRESH_SECONDS * 3)
    sentiment_cache = TimedCache(config.REFRESH_SECONDS * 5)
    factors_cache = TimedCache(config.REFRESH_SECONDS * 30)
    stability_tracker = TopKDropoutTracker(config.STATE_PATH, keep_k=10, buffer_k=20)
    validation_store = StrategyValidationStore(config.VALIDATION_DB_PATH)
    paper_store = PaperTradingStore(config.PAPER_TRADING_DB_PATH)

    # 验证指标按 (strategy, days) 缓存：每次 /api/recommendations 刷新会触发多次
    # validation_store.metrics() 的 sqlite JOIN，验证数据仅在手动更新时变化，
    # 故在刷新周期内复用结果即可消除热路径上的重复查询。
    _metrics_cache: Dict[tuple, tuple] = {}

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

    def _attach_event_risk_layer(candidates: pd.DataFrame) -> pd.DataFrame:
        payload = load_event_risk(provider)
        candidates = attach_event_risk(candidates, payload)
        codes = candidates["code"].tolist() if candidates is not None and "code" in candidates.columns else []
        return attach_fundamental_factors(candidates, load_fundamentals(provider, codes=codes))

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
    }

    def _configured_auto_update_strategies() -> List[str]:
        valid = [item["name"] for item in STRATEGY_CATALOG]
        configured = [
            item.strip()
            for item in str(getattr(config, "VALIDATION_AUTO_UPDATE_STRATEGIES", "")).split(",")
            if item.strip()
        ]
        selected = [item for item in configured if item in valid]
        return selected or valid

    def _code_batches(codes: List[str], batch_size: int) -> List[List[str]]:
        size = max(1, int(batch_size))
        return [codes[index:index + size] for index in range(0, len(codes), size)]

    def _set_auto_update_status(**values):
        with auto_update_lock:
            auto_update_status.update(values)

    def run_validation_auto_update_once() -> Dict[str, object]:
        if not config.VALIDATION_AUTO_UPDATE_ENABLED:
            return {"ok": True, "status": "disabled"}
        with auto_update_lock:
            if auto_update_status.get("running"):
                return {"ok": True, "status": "already_running"}
            auto_update_status["running"] = True
            auto_update_status["last_started_at"] = datetime.now().isoformat(timespec="seconds")
            auto_update_status["last_error"] = ""

        started_at = datetime.now().isoformat(timespec="seconds")
        result = {
            "ok": True,
            "started_at": started_at,
            "strategies": [],
            "totals": {"codes": 0, "batches": 0, "downloaded": 0, "cached": 0, "failed": 0, "updated": 0, "skipped": 0},
        }
        try:
            max_codes = max(1, int(config.VALIDATION_AUTO_UPDATE_MAX_CODES_PER_RUN))
            batch_size = max(1, int(config.VALIDATION_AUTO_UPDATE_BATCH_SIZE))
            days = max(30, int(config.VALIDATION_AUTO_UPDATE_HISTORY_DAYS))
            for strategy in _configured_auto_update_strategies():
                code_rows = validation_store.signal_codes(strategy_name=strategy, limit=max_codes)
                codes = [row["code"] for row in code_rows if row.get("code")]
                strategy_result = {
                    "strategy": strategy,
                    "code_count": len(codes),
                    "batches": [],
                    "updated": 0,
                    "skipped": 0,
                }
                for batch_index, batch_codes in enumerate(_code_batches(codes, batch_size), start=1):
                    prefetch = provider.prefetch_history(batch_codes, days=days, force=False)
                    outcome = validation_store.update_outcomes(
                        provider,
                        strategy_name=strategy,
                        codes=batch_codes,
                    )
                    batch_result = {
                        "batch": batch_index,
                        "code_count": len(batch_codes),
                        "prefetch": prefetch,
                        "outcome": outcome,
                    }
                    strategy_result["batches"].append(batch_result)
                    strategy_result["updated"] += int(outcome.get("updated") or 0)
                    strategy_result["skipped"] += int(outcome.get("skipped") or 0)
                    result["totals"]["codes"] += len(batch_codes)
                    result["totals"]["batches"] += 1
                    result["totals"]["downloaded"] += int(prefetch.get("downloaded") or 0)
                    result["totals"]["cached"] += int(prefetch.get("cached") or 0)
                    result["totals"]["failed"] += int(prefetch.get("failed") or 0)
                    result["totals"]["updated"] += int(outcome.get("updated") or 0)
                    result["totals"]["skipped"] += int(outcome.get("skipped") or 0)
                result["strategies"].append(strategy_result)
            invalidate_metrics_cache()
            factors_cache.clear()
            result["finished_at"] = datetime.now().isoformat(timespec="seconds")
            _set_auto_update_status(
                running=False,
                last_finished_at=result["finished_at"],
                last_result=result,
            )
            return result
        except Exception as exc:
            result["ok"] = False
            result["error"] = str(exc)
            result["finished_at"] = datetime.now().isoformat(timespec="seconds")
            _set_auto_update_status(
                running=False,
                last_finished_at=result["finished_at"],
                last_error=str(exc),
                last_result=result,
            )
            return result

    def _start_validation_auto_update_worker() -> None:
        if not config.VALIDATION_AUTO_UPDATE_ENABLED:
            return
        worker_key = "{}|{}".format(config.VALIDATION_DB_PATH, config.HISTORY_CACHE_PATH)
        with _VALIDATION_AUTO_WORKERS_LOCK:
            if worker_key in _VALIDATION_AUTO_WORKERS:
                return
            _VALIDATION_AUTO_WORKERS.add(worker_key)

        def _worker_loop():
            initial_delay = max(0, int(config.VALIDATION_AUTO_UPDATE_INITIAL_DELAY_SECONDS))
            if initial_delay:
                time.sleep(initial_delay)
            while True:
                run_validation_auto_update_once()
                interval = max(60, int(config.VALIDATION_AUTO_UPDATE_INTERVAL_SECONDS))
                _set_auto_update_status(next_run_after_seconds=interval)
                time.sleep(interval)

        _set_auto_update_status(started=True)
        thread = threading.Thread(target=_worker_loop, name="validation-auto-update", daemon=True)
        thread.start()

    _start_validation_auto_update_worker()

    @app.route("/")
    def index():
        return render_template(
            "index.html",
            refresh_seconds=config.REFRESH_SECONDS,
            default_top_n=config.DEFAULT_TOP_N,
        )

    @app.route("/api/strategy-overview")
    def strategy_overview():
        days = _int_arg("days", 20, minimum=1, maximum=120)
        try:
            quotes = quotes_cache.get()
            if quotes is None:
                quotes = provider.get_realtime_quotes()
                quotes_cache.set(quotes)
            candidates = _attach_event_risk_layer(prepare_candidates(quotes))
            market_regime = build_market_regime(candidates, breadth_source=quotes)
            strategies = []
            for item in STRATEGY_CATALOG:
                metrics = cached_metrics(item["name"], days)
                dates = validation_store.list_signal_dates(item["name"])
                latest = dates[0] if dates else {}
                status = _strategy_status(metrics)
                strategies.append(
                    {
                        **item,
                        "metrics": metrics,
                        "latest_signal": latest,
                        "status": status,
                    }
                )
            save_strategy_status({row["name"]: row["status"] for row in strategies})
            ranked = sorted(
                strategies,
                key=lambda row: (
                    row["metrics"].get("real_sample_count", 0) > 0,
                    row["metrics"].get("real_avg_primary_return_net", row["metrics"].get("avg_primary_return_net", -999)),
                    row["metrics"].get("real_win_rate_primary_net", row["metrics"].get("win_rate_primary_net", -999)),
                ),
                reverse=True,
            )
            return jsonify(
                {
                    "ok": True,
                    "days": days,
                    "strategies": strategies,
                    "best_strategy": ranked[0] if ranked and ranked[0]["metrics"].get("sample_count", 0) else None,
                    "market_regime": market_regime,
                    "health": provider.health(),
                    "disclaimer": "仅供研究，不构成投资建议。",
                }
            )
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc), "health": provider.health()}), 502

    @app.route("/api/portfolio")
    def portfolio():
        strategy = request.args.get("strategy", "tomorrow_picks")
        if strategy not in SNAPSHOT_STRATEGIES:
            strategy = "tomorrow_picks"
        try:
            rows = validation_store.latest_signal_rows(strategy)
            performance = paper_store.performance(strategy, days=120)
            market_regime = {}
            try:
                quotes = quotes_cache.get()
                if quotes is None:
                    quotes = provider.get_realtime_quotes()
                    quotes_cache.set(quotes)
                market_regime = build_market_regime(prepare_candidates(quotes), breadth_source=quotes)
            except Exception:
                market_regime = {}
            result = build_portfolio(rows, market_regime=market_regime, performance=performance)
            no_trade_reason = ""
            if not rows:
                no_trade_reason = "暂无保存快照，先在策略验证中保存该策略预测。"
            elif not result["rows"]:
                no_trade_reason = "最近快照没有可配置仓位的标的。"
            elif result["summary"].get("constraints_feasible") is False:
                no_trade_reason = "候选票或主题分散度不足，剩余仓位保留现金。"
            return jsonify(
                {
                    "ok": True,
                    "strategy": strategy,
                    "data": result["rows"],
                    "exposure": result["exposure"],
                    "summary": result["summary"],
                    "cash_weight": result["summary"].get("cash_pct", 100.0),
                    "no_trade_reason": no_trade_reason,
                    "empty_reason": no_trade_reason if not rows else "",
                    "performance": performance,
                    "market_regime": market_regime,
                    "health": provider.health(),
                    "disclaimer": "仅供研究，不构成投资建议。",
                }
            )
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc), "health": provider.health()}), 502

    @app.route("/api/portfolio/performance")
    def portfolio_performance():
        strategy = request.args.get("strategy", "all")
        if strategy != "all" and strategy not in SNAPSHOT_STRATEGIES:
            strategy = "all"
        days = _int_arg("days", 120, minimum=1, maximum=500)
        try:
            return jsonify(
                {
                    "ok": True,
                    "strategy": strategy,
                    "days": days,
                    "performance": paper_store.performance(strategy, days=days),
                    "health": provider.health(),
                    "disclaimer": "纸面组合仅供研究，不构成投资建议。",
                }
            )
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc), "health": provider.health()}), 502

    @app.route("/api/paper-trades")
    def paper_trades():
        strategy = request.args.get("strategy", "all")
        if strategy != "all" and strategy not in SNAPSHOT_STRATEGIES:
            strategy = "all"
        limit = _int_arg("limit", 200, minimum=1, maximum=1000)
        try:
            return jsonify(
                {
                    "ok": True,
                    "strategy": strategy,
                    "data": paper_store.trades(strategy, limit=limit),
                    "health": provider.health(),
                    "disclaimer": "纸面交易仅供研究，不构成投资建议。",
                }
            )
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc), "health": provider.health()}), 502

    @app.route("/api/recommendations")
    def recommendations():
        top_n = _int_arg("top_n", 10, minimum=5, maximum=50)
        market = request.args.get("market", "all")
        if market not in ("all", "main", "chinext", "star"):
            market = "all"

        try:
            quotes = quotes_cache.get()
            if quotes is None:
                quotes = provider.get_realtime_quotes()
                quotes_cache.set(quotes)
            candidates = _attach_event_risk_layer(prepare_candidates(quotes))
            candidates = _attach_alphalite_factors(provider, factors_cache, candidates)
            market_regime = build_market_regime(candidates, breadth_source=quotes)

            hot_ranks = hot_cache.get()
            if hot_ranks is None:
                if config.ENABLE_HOT_RANKS:
                    try:
                        hot_ranks = provider.get_hot_ranks()
                    except Exception:
                        hot_ranks = {}
                else:
                    hot_ranks = {}
                hot_cache.set(hot_ranks)

            industry_strength = industry_cache.get()
            if industry_strength is None:
                if config.ENABLE_INDUSTRY_STRENGTH:
                    try:
                        industry_strength = provider.get_industry_strength()
                    except Exception:
                        industry_strength = {}
                else:
                    industry_strength = {}
                industry_cache.set(industry_strength)

            candidate_subset = candidates.sort_values("pct_chg", ascending=False).head(80)
            sentiment_lookup = _sentiment_for_candidates(
                provider,
                sentiment_cache,
                candidate_subset[["code", "name"]].to_dict("records"),
            )

            recommendations_by_horizon, meta = score_dual_horizon_candidates(
                candidates,
                hot_ranks=hot_ranks,
                industry_strength=industry_strength,
                sentiment_lookup=sentiment_lookup,
                top_n=max(top_n, 30),
                market_filter=market,
                market_regime=market_regime,
            )
            tomorrow_rows, tomorrow_meta = score_tomorrow_candidates(
                candidates,
                top_n=30,
                market_filter=market,
                market_regime=market_regime,
            )
            swing_rows, swing_meta = score_swing_candidates(
                candidates,
                top_n=30,
                market_filter=market,
                market_regime=market_regime,
            )
            position_rows, position_meta = score_position_candidates(
                candidates,
                top_n=30,
                market_filter=market,
                market_regime=market_regime,
            )
            tech_rows, tech_meta = score_tech_potential_candidates(
                candidates,
                top_n=30,
                market_filter=market,
                market_regime=market_regime,
            )
            short_stability = stability_tracker.update("short_term", recommendations_by_horizon["short_term"])
            long_stability = stability_tracker.update("long_term", recommendations_by_horizon["long_term"])
            recommendations_by_horizon = {
                "short_term": short_stability["rows"][:top_n],
                "long_term": long_stability["rows"][:top_n],
            }
            _attach_validation_summary(recommendations_by_horizon["short_term"], validation_store, "short_term", metrics_fn=cached_metrics)
            _attach_validation_summary(recommendations_by_horizon["long_term"], validation_store, "long_term", metrics_fn=cached_metrics)
            meta["top_n"] = top_n
            meta["stability"] = {
                "short_term": {
                    "new_entries": short_stability["new_entries"],
                    "dropped": short_stability["dropped"],
                    "retained": short_stability["retained"],
                    "last_updated": short_stability["last_updated"],
                },
                "long_term": {
                    "new_entries": long_stability["new_entries"],
                    "dropped": long_stability["dropped"],
                    "retained": long_stability["retained"],
                    "last_updated": long_stability["last_updated"],
                },
            }
            meta["market_regime"] = market_regime
            # B2：用各策略近期验证命中率作为共识可信度乘子（失败/无数据则空字典，安全回退）。
            strategy_metrics = {}
            for strategy_key in (
                "short_term", "long_term", "tomorrow_picks",
                "swing_picks", "position_picks", "tech_potential",
            ):
                try:
                    strategy_metrics[strategy_key] = cached_metrics(strategy_key, 20)
                except Exception:
                    pass
            consensus_rows = build_strategy_consensus(
                {
                    "short_term": short_stability["rows"],
                    "long_term": long_stability["rows"],
                    "tomorrow_picks": tomorrow_rows,
                    "swing_picks": swing_rows,
                    "position_picks": position_rows,
                    "tech_potential": tech_rows,
                },
                minimum_appearances=2,
                top_n=30,
                strategy_metrics=strategy_metrics,
            )
            meta["strategy_consensus"] = {
                "rows": consensus_rows,
                "strategy_count": 6,
                "serenity_references": SERENITY_REFERENCES,
                "trading_agents_reference": TRADING_AGENTS_REFERENCE,
                "source_versions": {
                    "short_term": "dual_horizon_v2",
                    "long_term": "dual_horizon_v2",
                    "tomorrow_picks": tomorrow_meta.get("strategy_version", "tomorrow_picks_v2"),
                    "swing_picks": swing_meta.get("strategy_version", "swing_5_10d_v1"),
                    "position_picks": position_meta.get("strategy_version", "position_1_3m_v1"),
                    "tech_potential": tech_meta.get("strategy_version", "tech_potential_v1"),
                },
            }
            consensus_lookup = {row["code"]: row for row in consensus_rows}
            for horizon_name in ("short_term", "long_term"):
                for row in recommendations_by_horizon[horizon_name]:
                    consensus = consensus_lookup.get(row.get("code"))
                    if consensus:
                        row["consensus_signal"] = consensus

            market_news = _market_news(provider, market_news_cache)

            return jsonify(
                {
                    "ok": True,
                    "data": recommendations_by_horizon["short_term"],
                    "recommendations": recommendations_by_horizon,
                    "meta": meta,
                    "market_sentiment": build_market_sentiment_index(market_news),
                    "health": provider.health(),
                    "disclaimer": "仅供研究，不构成投资建议。",
                }
            )
        except Exception as exc:
            saved_rows = validation_store.latest_signal_rows("tomorrow_picks")
            if saved_rows:
                _attach_validation_summary(saved_rows, validation_store, "tomorrow_picks", metrics_fn=cached_metrics)
                return jsonify(
                    {
                        "ok": True,
                        "data": saved_rows[:top_n],
                        "meta": {
                            "generated_at": "",
                            "candidate_count": len(saved_rows),
                            "top_n": top_n,
                            "market_filter": market,
                            "strategy": "实时行情不可用，显示最近保存的14:30预测",
                            "fallback": "saved_snapshot",
                        },
                        "health": provider.health(),
                        "disclaimer": "仅供研究，不构成投资建议。",
                    }
                )
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": str(exc),
                        "health": provider.health(),
                        "disclaimer": "仅供研究，不构成投资建议。",
                    }
                ),
                502,
            )

    @app.route("/api/sentiment/<code>")
    def sentiment(code: str):
        name = request.args.get("name", "")
        normalized_code = code.strip()[:6]
        try:
            result = score_stock_sentiment(provider, normalized_code, name=name)
            return jsonify(
                {
                    "ok": True,
                    "code": normalized_code,
                    "name": name,
                    "sentiment": result,
                    "health": provider.health(),
                }
            )
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc), "health": provider.health()}), 502

    @app.route("/api/health")
    def health():
        coverage = {"row_count": 0, "avg_data_coverage": 0.0, "columns": {}, "degraded": True}
        try:
            quotes = quotes_cache.get()
            if quotes is None:
                quotes = provider.get_realtime_quotes()
                quotes_cache.set(quotes)
            candidates = _attach_event_risk_layer(prepare_candidates(quotes))
            candidates = _attach_alphalite_factors(provider, factors_cache, candidates)
            coverage = factor_coverage(candidates)
        except Exception:
            pass
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
                "factor_ic": {
                    "enabled": bool(config.ENABLE_FUNDAMENTALS),
                    "fundamentals_status": load_fundamentals(provider).get("status", "disabled"),
                    "generated_at": load_factor_ic().get("generated_at", ""),
                },
                "health": provider.health(),
            }
        )

    @app.route("/api/stock-prediction/<code>")
    def stock_prediction(code: str):
        normalized_code = code.strip()[:12]
        try:
            quote_error = ""
            quotes = quotes_cache.get()
            if quotes is None:
                try:
                    quotes = provider.get_realtime_quotes()
                    quotes_cache.set(quotes)
                except Exception as exc:
                    quote_error = str(exc)
                    quotes = pd.DataFrame()
            candidates = _attach_event_risk_layer(prepare_candidates(quotes))
            candidates = _attach_alphalite_factors_for_codes(provider, candidates, [normalized_code])
            market_regime = build_market_regime(candidates, breadth_source=quotes)
            top_n = max(1, len(candidates))
            dual_rows, dual_meta = score_dual_horizon_candidates(
                candidates,
                hot_ranks={},
                industry_strength={},
                sentiment_lookup={},
                top_n=top_n,
                market_regime=market_regime,
            )
            tomorrow_rows, tomorrow_meta = score_tomorrow_candidates(
                candidates,
                top_n=top_n,
                market_regime=market_regime,
            )
            swing_rows, swing_meta = score_swing_candidates(
                candidates,
                top_n=top_n,
                market_regime=market_regime,
            )
            position_rows, position_meta = score_position_candidates(
                candidates,
                top_n=top_n,
                market_regime=market_regime,
            )
            tech_rows, tech_meta = score_tech_potential_candidates(
                candidates,
                top_n=top_n,
                market_regime=market_regime,
            )
            chokepoint_rows, chokepoint_meta = score_chokepoint_candidates(
                candidates,
                top_n=top_n,
                market_regime=market_regime,
            )
            fallback_history = None
            fallback_error = ""
            normalized_for_lookup = normalize_code(normalized_code)
            if not _stock_exists_in_quotes(normalized_for_lookup, quotes):
                try:
                    fallback_history = provider.get_history(normalized_for_lookup, days=120)
                except Exception as exc:
                    fallback_error = str(exc)
            if quote_error:
                fallback_error = "; ".join(item for item in (quote_error, fallback_error) if item)
            result = build_stock_prediction(
                normalized_code,
                candidates,
                {
                    "short_term": dual_rows.get("short_term", []),
                    "long_term": dual_rows.get("long_term", []),
                    "tomorrow_picks": tomorrow_rows,
                    "swing_picks": swing_rows,
                    "position_picks": position_rows,
                    "tech_potential": tech_rows,
                    "chokepoint_picks": chokepoint_rows,
                },
                strategy_metas={
                    "short_term": dual_meta,
                    "long_term": dual_meta,
                    "tomorrow_picks": tomorrow_meta,
                    "swing_picks": swing_meta,
                    "position_picks": position_meta,
                    "tech_potential": tech_meta,
                    "chokepoint_picks": chokepoint_meta,
                },
                market_regime=market_regime,
                raw_quotes=quotes,
                fallback_history=fallback_history,
                fallback_error=fallback_error,
            )
            return jsonify({**result, "health": provider.health()})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc), "health": provider.health()}), 502

    @app.route("/api/tomorrow-picks")
    def tomorrow_picks():
        top_n = _int_arg("top_n", 50, minimum=10, maximum=50)
        market = request.args.get("market", "all")
        if market not in ("all", "main", "chinext", "star"):
            market = "all"
        try:
            quotes = quotes_cache.get()
            if quotes is None:
                quotes = provider.get_realtime_quotes()
                quotes_cache.set(quotes)
            candidates = _attach_event_risk_layer(prepare_candidates(quotes))
            market_regime = build_market_regime(candidates, breadth_source=quotes)
            rows, meta = score_tomorrow_candidates(
                candidates,
                top_n=top_n,
                market_filter=market,
                market_regime=market_regime,
            )
            meta["market_regime"] = market_regime
            _attach_validation_summary(rows, validation_store, "tomorrow_picks", metrics_fn=cached_metrics)
            return jsonify(
                {
                    "ok": True,
                    "data": rows,
                    "meta": meta,
                    "health": provider.health(),
                    "disclaimer": "仅供研究，不构成投资建议。",
                }
            )
        except Exception as exc:
            saved_rows = validation_store.latest_signal_rows("tomorrow_picks")
            if saved_rows:
                _attach_validation_summary(saved_rows, validation_store, "tomorrow_picks", metrics_fn=cached_metrics)
                return jsonify(
                    {
                        "ok": True,
                        "data": saved_rows[:top_n],
                        "meta": {
                            "generated_at": "",
                            "candidate_count": len(saved_rows),
                            "top_n": top_n,
                            "market_filter": market,
                            "analysis_window": "14:30",
                            "strategy_version": "tomorrow_picks_v2",
                            "strategy_label": "明天预测",
                            "strategy": "实时行情不可用，显示最近保存的明天预测",
                            "fallback": "saved_snapshot",
                            "policy": {
                                "main_max_gain": config.MAX_BUYABLE_GAIN_MAIN,
                                "growth_max_gain": config.MAX_BUYABLE_GAIN_GROWTH,
                                "min_turnover": config.MIN_TURNOVER,
                                "avoid_limit_up": True,
                            },
                        },
                        "health": provider.health(),
                        "disclaimer": "仅供研究，不构成投资建议。",
                    }
                )
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": str(exc),
                        "health": provider.health(),
                        "disclaimer": "仅供研究，不构成投资建议。",
                    }
                ),
                502,
            )

    @app.route("/api/tech-potential")
    def tech_potential():
        top_n = _int_arg("top_n", 50, minimum=10, maximum=50)
        market = request.args.get("market", "all")
        if market not in ("all", "main", "chinext", "star"):
            market = "all"
        try:
            quotes = quotes_cache.get()
            if quotes is None:
                quotes = provider.get_realtime_quotes()
                quotes_cache.set(quotes)
            candidates = _attach_event_risk_layer(prepare_candidates(quotes))
            market_regime = build_market_regime(candidates, breadth_source=quotes)
            rows, meta = score_tech_potential_candidates(
                candidates,
                top_n=top_n,
                market_filter=market,
                market_regime=market_regime,
            )
            meta["market_regime"] = market_regime
            _attach_validation_summary(rows, validation_store, "tech_potential", metrics_fn=cached_metrics)
            return jsonify(
                {
                    "ok": True,
                    "data": rows,
                    "meta": meta,
                    "health": provider.health(),
                    "disclaimer": "仅供研究，不构成投资建议。",
                }
            )
        except Exception as exc:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": str(exc),
                        "health": provider.health(),
                        "disclaimer": "仅供研究，不构成投资建议。",
                    }
                ),
                502,
            )

    @app.route("/api/chokepoint-picks")
    def chokepoint_picks():
        top_n = _int_arg("top_n", 30, minimum=10, maximum=50)
        market = request.args.get("market", "all")
        if market not in ("all", "main", "chinext", "star"):
            market = "all"
        try:
            quotes = quotes_cache.get()
            if quotes is None:
                quotes = provider.get_realtime_quotes()
                quotes_cache.set(quotes)
            candidates = _attach_event_risk_layer(prepare_candidates(quotes))
            market_regime = build_market_regime(candidates, breadth_source=quotes)
            rows, meta = score_chokepoint_candidates(
                candidates,
                top_n=top_n,
                market_filter=market,
                market_regime=market_regime,
            )
            meta["market_regime"] = market_regime
            meta["industry_map"] = _chokepoint_industry_map(candidates, rows, quotes, market_regime)
            if not rows:
                meta["empty_reason"] = "当前实时候选股没有命中卡脖子上游关键词；免费行情的行业字段可能为空或过粗，先看下方行业目录和龙头状态。"
            _attach_validation_summary(rows, validation_store, "chokepoint_picks", metrics_fn=cached_metrics)
            return jsonify(
                {
                    "ok": True,
                    "data": rows,
                    "meta": meta,
                    "health": provider.health(),
                    "disclaimer": "仅供研究，不构成投资建议。",
                }
            )
        except Exception as exc:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": str(exc),
                        "health": provider.health(),
                        "disclaimer": "仅供研究，不构成投资建议。",
                    }
                ),
                502,
            )

    @app.route("/api/swing-picks")
    def swing_picks():
        top_n = _int_arg("top_n", 30, minimum=10, maximum=50)
        market = request.args.get("market", "all")
        if market not in ("all", "main", "chinext", "star"):
            market = "all"
        try:
            quotes = quotes_cache.get()
            if quotes is None:
                quotes = provider.get_realtime_quotes()
                quotes_cache.set(quotes)
            candidates = _attach_event_risk_layer(prepare_candidates(quotes))
            candidates = _attach_alphalite_factors(provider, factors_cache, candidates)
            market_regime = build_market_regime(candidates, breadth_source=quotes)
            rows, meta = score_swing_candidates(
                candidates,
                top_n=top_n,
                market_filter=market,
                market_regime=market_regime,
            )
            meta["market_regime"] = market_regime
            _attach_validation_summary(rows, validation_store, "swing_picks", metrics_fn=cached_metrics)
            return jsonify(
                {
                    "ok": True,
                    "data": rows,
                    "meta": meta,
                    "health": provider.health(),
                    "disclaimer": "仅供研究，不构成投资建议。",
                }
            )
        except Exception as exc:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": str(exc),
                        "health": provider.health(),
                        "disclaimer": "仅供研究，不构成投资建议。",
                    }
                ),
                502,
            )

    def _factor_strategy_route(strategy_name, scorer, default_top_n):
        """反转/小市值/量价突破共用：附 AlphaLite 因子→打分→附验证→标准 JSON。"""
        top_n = _int_arg("top_n", default_top_n, minimum=10, maximum=50)
        market = request.args.get("market", "all")
        if market not in ("all", "main", "chinext", "star"):
            market = "all"
        try:
            quotes = quotes_cache.get()
            if quotes is None:
                quotes = provider.get_realtime_quotes()
                quotes_cache.set(quotes)
            candidates = _attach_event_risk_layer(prepare_candidates(quotes))
            candidates = _attach_alphalite_factors(provider, factors_cache, candidates)
            market_regime = build_market_regime(candidates, breadth_source=quotes)
            rows, meta = scorer(
                candidates,
                top_n=top_n,
                market_filter=market,
                market_regime=market_regime,
            )
            meta["market_regime"] = market_regime
            _attach_validation_summary(rows, validation_store, strategy_name, metrics_fn=cached_metrics)
            return jsonify(
                {
                    "ok": True,
                    "data": rows,
                    "meta": meta,
                    "health": provider.health(),
                    "disclaimer": "仅供研究，不构成投资建议。",
                }
            )
        except Exception as exc:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": str(exc),
                        "health": provider.health(),
                        "disclaimer": "仅供研究，不构成投资建议。",
                    }
                ),
                502,
            )

    @app.route("/api/reversal-picks")
    def reversal_picks():
        return _factor_strategy_route("reversal_picks", score_reversal_candidates, 30)

    @app.route("/api/smallcap-value-picks")
    def smallcap_value_picks():
        return _factor_strategy_route("smallcap_value_picks", score_smallcap_value_candidates, 30)

    @app.route("/api/breakout-picks")
    def breakout_picks():
        return _factor_strategy_route("breakout_picks", score_breakout_candidates, 30)

    @app.route("/api/position-picks")
    def position_picks():
        top_n = _int_arg("top_n", 30, minimum=10, maximum=50)
        market = request.args.get("market", "all")
        if market not in ("all", "main", "chinext", "star"):
            market = "all"
        try:
            quotes = quotes_cache.get()
            if quotes is None:
                quotes = provider.get_realtime_quotes()
                quotes_cache.set(quotes)
            candidates = _attach_event_risk_layer(prepare_candidates(quotes))
            candidates = _attach_alphalite_factors(provider, factors_cache, candidates)
            market_regime = build_market_regime(candidates, breadth_source=quotes)
            rows, meta = score_position_candidates(
                candidates,
                top_n=top_n,
                market_filter=market,
                market_regime=market_regime,
            )
            meta["market_regime"] = market_regime
            _attach_validation_summary(rows, validation_store, "position_picks", metrics_fn=cached_metrics)
            return jsonify(
                {
                    "ok": True,
                    "data": rows,
                    "meta": meta,
                    "health": provider.health(),
                    "disclaimer": "仅供研究，不构成投资建议。",
                }
            )
        except Exception as exc:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": str(exc),
                        "health": provider.health(),
                        "disclaimer": "仅供研究，不构成投资建议。",
                    }
                ),
                502,
            )

    @app.route("/api/strategy-validation/snapshot", methods=["POST"])
    def strategy_snapshot():
        strategy = request.args.get("strategy", "tech_potential")
        market = request.args.get("market", "all")
        if strategy not in SNAPSHOT_STRATEGIES:
            strategy = "tech_potential"
        if market not in ("all", "main", "chinext", "star"):
            market = "all"
        try:
            result = run_snapshot(provider, validation_store, strategy, market=market)
            invalidate_metrics_cache()
            return jsonify({"ok": bool(result.get("ok")), **result, "health": provider.health()})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc), "health": provider.health()}), 502

    @app.route("/api/strategy-validation/update", methods=["POST"])
    def strategy_validation_update():
        signal_date = request.args.get("date", "")
        strategy = request.args.get("strategy", "")
        try:
            result = validation_store.update_outcomes(
                provider,
                signal_date=signal_date,
                strategy_name=strategy,
            )
            invalidate_metrics_cache()
            return jsonify({"ok": True, "result": result, "health": provider.health()})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc), "health": provider.health()}), 502

    @app.route("/api/strategy-validation/auto-update-status")
    def strategy_validation_auto_update_status():
        with auto_update_lock:
            status = dict(auto_update_status)
        status["config"] = {
            "initial_delay_seconds": config.VALIDATION_AUTO_UPDATE_INITIAL_DELAY_SECONDS,
            "interval_seconds": config.VALIDATION_AUTO_UPDATE_INTERVAL_SECONDS,
            "batch_size": config.VALIDATION_AUTO_UPDATE_BATCH_SIZE,
            "max_codes_per_run": config.VALIDATION_AUTO_UPDATE_MAX_CODES_PER_RUN,
            "history_days": config.VALIDATION_AUTO_UPDATE_HISTORY_DAYS,
            "strategies": _configured_auto_update_strategies(),
        }
        return jsonify({"ok": True, "auto_update": status, "health": provider.health()})

    @app.route("/api/strategy-validation/prefetch-history", methods=["POST"])
    def strategy_validation_prefetch_history():
        signal_date = request.args.get("date", "")
        strategy = request.args.get("strategy", "")
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
            return jsonify(
                {
                    "ok": True,
                    "codes": code_rows,
                    "prefetch": prefetch,
                    "outcome": outcome,
                    "health": provider.health(),
                }
            )
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc), "health": provider.health()}), 502

    @app.route("/api/strategy-validation/backfill-samples", methods=["POST"])
    def strategy_validation_backfill_samples():
        strategy = request.args.get("strategy", "tomorrow_picks")
        if strategy not in ("tomorrow_picks", "swing_picks", "position_picks", "tech_potential", "chokepoint_picks", "reversal_picks", "smallcap_value_picks", "breakout_picks"):
            strategy = "tomorrow_picks"
        days = _int_arg("days", 260, minimum=80, maximum=600)
        replay_days = _int_arg("replay_days", 20, minimum=1, maximum=80)
        top_n = _int_arg("top_n", 30, minimum=1, maximum=50)
        holding_days = _int_arg("holding_days", 3, minimum=1, maximum=20)
        limit = _int_arg("limit", 120, minimum=10, maximum=500)
        force = request.args.get("force", "0") in ("1", "true", "yes")
        try:
            code_rows = validation_store.signal_codes(strategy_name=strategy, limit=limit)
            if not code_rows:
                code_rows = _candidate_code_rows(provider, quotes_cache, limit)
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
            return jsonify(
                {
                    "ok": bool(replay.get("ok")),
                    "codes": code_rows,
                    "prefetch": prefetch,
                    "replay": replay,
                    "metrics": metrics,
                    "health": provider.health(),
                }
            )
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc), "health": provider.health()}), 502

    @app.route("/api/strategy-validation")
    def strategy_validation():
        strategy = request.args.get("strategy", "")
        days = _int_arg("days", 20, minimum=1, maximum=120)
        try:
            return jsonify(
                {
                    "ok": True,
                    "dates": validation_store.list_signal_dates(strategy),
                    "metrics": validation_store.metrics(strategy, days=days),
                    "health": provider.health(),
                }
            )
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc), "health": provider.health()}), 502

    @app.route("/api/strategy-validation/daily")
    def strategy_validation_daily():
        signal_date = request.args.get("date", "")
        strategy = request.args.get("strategy", "")
        if not signal_date:
            return jsonify({"ok": False, "error": "缺少 date 参数"}), 400
        try:
            return jsonify(
                {
                    "ok": True,
                    "date": signal_date,
                    "data": validation_store.signals_for_date(signal_date, strategy),
                    "health": provider.health(),
                }
            )
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc), "health": provider.health()}), 502

    @app.route("/api/validation-overview")
    def validation_overview():
        """B3：各策略主周期净胜率时间序列 + 聚合指标，供前端折线图消费。"""
        days = _int_arg("days", 20, minimum=1, maximum=120)
        strategies = [
            "short_term", "long_term", "tomorrow_picks",
            "swing_picks", "position_picks", "tech_potential", "chokepoint_picks",
            "reversal_picks", "smallcap_value_picks", "breakout_picks",
        ]
        try:
            series = []
            for name in strategies:
                metrics = cached_metrics(name, days)
                daily = list(reversed(metrics.get("daily", [])))  # 时间升序便于画图
                series.append(
                    {
                        "strategy": name,
                        "label": STRATEGY_LABELS.get(name, name),
                        "win_rate_next_close": metrics.get("win_rate_next_close"),
                        "hit_3pct_rate": metrics.get("hit_3pct_rate"),
                        "avg_next_close_return": metrics.get("avg_next_close_return"),
                        "win_rate_primary_net": metrics.get("win_rate_primary_net"),
                        "avg_primary_return_net": metrics.get("avg_primary_return_net"),
                        "real_win_rate_primary_net": metrics.get("real_win_rate_primary_net"),
                        "real_avg_primary_return_net": metrics.get("real_avg_primary_return_net"),
                        "primary_horizon_label": metrics.get("primary_horizon_label"),
                        "sample_count": metrics.get("sample_count", 0),
                        "real_sample_count": metrics.get("real_sample_count", 0),
                        "replay_sample_count": metrics.get("replay_sample_count", 0),
                        "daily": [
                            {
                                "date": item.get("signal_date"),
                                "win_rate": item.get("win_rate_primary_net", item.get("win_rate_next_close")),
                                "hit_3pct": item.get("hit_3pct_rate"),
                                "avg_return": item.get("avg_primary_return_net", item.get("avg_next_close_return")),
                                "sample_count": item.get("sample_count", 0),
                                "real_sample_count": item.get("real_sample_count", 0),
                                "replay_sample_count": item.get("replay_sample_count", 0),
                            }
                            for item in daily
                        ],
                    }
                )
            return jsonify({"ok": True, "days": days, "series": series, "health": provider.health()})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc), "health": provider.health()}), 502

    @app.route("/api/backtest")
    def backtest():
        codes = parse_code_list(request.args.get("codes", ""))
        if not codes:
            try:
                quotes = quotes_cache.get()
                if quotes is None:
                    quotes = provider.get_realtime_quotes()
                candidates = _attach_event_risk_layer(prepare_candidates(quotes))
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
        history_by_code = {}
        for code in codes[:60]:
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


def _candidate_code_rows(provider, quotes_cache: TimedCache, limit: int) -> list:
    quotes = quotes_cache.get()
    if quotes is None:
        quotes = provider.get_realtime_quotes()
        quotes_cache.set(quotes)
    candidates = attach_event_risk(prepare_candidates(quotes), load_event_risk(provider))
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


def _chokepoint_industry_map(candidates, rows, raw_quotes, market_regime):
    row_lookup = {normalize_code(row.get("code")): row for row in rows or []}
    raw_lookup = _quote_lookup(raw_quotes)
    items = []
    totals = {
        "industry_count": 0,
        "leader_count": 0,
        "unique_leader_count": 0,
        "recommended_count": 0,
        "matched_count": 0,
        "quote_available_count": 0,
    }
    unique_codes = set()
    recommended_codes = set()
    matched_codes = set()
    quoted_codes = set()
    for segment, leaders in CHOKEPOINT_INDUSTRY_LEADERS.items():
        leader_rows = []
        recommended_count = 0
        for leader in leaders:
            code = normalize_code(leader.get("code"))
            scored = row_lookup.get(code)
            quote = raw_lookup.get(code, {})
            item = _leader_status(segment, leader, scored, quote, market_regime)
            if item["recommendation"]["level"] in ("buy", "watch"):
                recommended_count += 1
                recommended_codes.add(code)
            if item.get("matched"):
                matched_codes.add(code)
            if item.get("quote_available"):
                quoted_codes.add(code)
            unique_codes.add(code)
            leader_rows.append(item)
        items.append(
            {
                "segment": segment,
                "leader_count": len(leader_rows),
                "recommended_count": recommended_count,
                "leaders": leader_rows,
            }
        )
        totals["industry_count"] += 1
        totals["leader_count"] += len(leader_rows)
    totals["unique_leader_count"] = len(unique_codes)
    totals["recommended_count"] = len(recommended_codes)
    totals["matched_count"] = len(matched_codes)
    totals["quote_available_count"] = len(quoted_codes)
    for item in items:
        item["totals"] = totals
    return items


def _quote_lookup(quotes) -> Dict[str, Dict[str, object]]:
    if quotes is None or quotes.empty:
        return {}
    try:
        from .normalization import rename_known_columns

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


def _stock_exists_in_quotes(code: str, quotes) -> bool:
    if quotes is None or quotes.empty:
        return False
    try:
        from .normalization import rename_known_columns

        df = rename_known_columns(quotes.copy())
    except Exception:
        df = quotes.copy()
    if "code" not in df.columns:
        return False
    return normalize_code(code) in set(df["code"].map(normalize_code).astype(str))


def _leader_status(segment: str, leader: Dict[str, object], scored: Dict[str, object], quote: Dict[str, object], market_regime: Dict[str, object]) -> Dict[str, object]:
    code = normalize_code(leader.get("code"))
    name = str((scored or {}).get("name") or (quote or {}).get("name") or leader.get("name") or "")
    if scored:
        profile = scored.get("serenity_profile") or {}
        committee = scored.get("agent_committee") or {}
        action = committee.get("final_action_label") or profile.get("action_label") or "观察"
        recommendation = _leader_recommendation(
            score=coerce_number(scored.get("score")),
            risk=coerce_number(profile.get("risk_score"), 50.0),
            action=action,
            matched=True,
        )
        return {
            "code": code,
            "name": name,
            "segment": segment,
            "matched": True,
            "quote_available": True,
            "price": coerce_number(scored.get("price")),
            "pct_chg": coerce_number(scored.get("pct_chg")),
            "turnover": coerce_number(scored.get("turnover")),
            "score": coerce_number(scored.get("score")),
            "rank": scored.get("rank"),
            "action_label": action,
            "verdict": scored.get("verdict") or {},
            "reasons": list(scored.get("reasons") or [])[:3],
            "recommendation": recommendation,
        }

    if quote:
        pct = coerce_number(quote.get("pct_chg"))
        turnover = coerce_number(quote.get("turnover"))
        risks = []
        if turnover < config.MIN_TURNOVER:
            risks.append("成交额不足")
        if pct > config.MAX_RECOMMENDED_GAIN:
            risks.append("当日涨幅过高")
        if pct <= -8:
            risks.append("当日跌幅过大")
        if not risks:
            risks.append("未命中卡脖子关键词或未进入当前策略榜")
        action = "只观察" if risks else "等待确认"
        return {
            "code": code,
            "name": name,
            "segment": segment,
            "matched": False,
            "quote_available": True,
            "price": coerce_number(quote.get("price")),
            "pct_chg": pct,
            "turnover": turnover,
            "score": 0.0,
            "rank": None,
            "action_label": action,
            "verdict": {},
            "reasons": risks[:3],
            "recommendation": {
                "level": "avoid" if pct > config.MAX_RECOMMENDED_GAIN or turnover < config.MIN_TURNOVER else "observe",
                "label": "不建议买入" if pct > config.MAX_RECOMMENDED_GAIN or turnover < config.MIN_TURNOVER else "仅观察",
                "reason": "；".join(risks[:3]),
            },
        }

    return {
        "code": code,
        "name": name,
        "segment": segment,
        "matched": False,
        "quote_available": False,
        "price": 0.0,
        "pct_chg": 0.0,
        "turnover": 0.0,
        "score": 0.0,
        "rank": None,
        "action_label": "无行情",
        "verdict": {},
        "reasons": ["当前行情源未返回该股票"],
        "recommendation": {
            "level": "unknown",
            "label": "无法判断",
            "reason": "当前行情源未返回该股票，可能停牌、代码不在免费源或行情延迟。",
        },
    }


def _leader_recommendation(score: float, risk: float, action: str, matched: bool) -> Dict[str, str]:
    if not matched:
        return {"level": "observe", "label": "仅观察", "reason": "未进入当前卡脖子策略榜。"}
    if "风控否决" in action or "只观察" in action or risk >= 72:
        return {"level": "avoid", "label": "不建议买入", "reason": "风险分偏高或风控动作偏谨慎。"}
    if score >= 72 and risk <= 55 and ("批准" in action or "优先" in action):
        return {"level": "buy", "label": "可加入买入观察", "reason": "已命中卡脖子策略且质量/风险组合较好；仍需仓位和止损。"}
    if score >= 60 and risk <= 65:
        return {"level": "watch", "label": "小仓观察", "reason": "有正向信号但不够强，等待回踩或更多确认。"}
    return {"level": "observe", "label": "仅观察", "reason": "分数或风险收益比不足，暂不建议主动买入。"}


def _sentiment_for_candidates(provider, cache: TimedCache, candidates) -> Dict[str, Dict[str, object]]:
    if not config.ENABLE_INLINE_SENTIMENT:
        return {}
    cached = cache.get()
    if cached is not None:
        return cached
    lookup: Dict[str, Dict[str, object]] = {}
    for item in candidates[:30]:
        code = item.get("code")
        if not code:
            continue
        try:
            lookup[code] = score_stock_sentiment(provider, code, name=item.get("name", ""))
        except Exception:
            lookup[code] = {"score": 50.0, "summary": "舆情接口暂不可用", "risk_words": []}
    cache.set(lookup)
    return lookup


def _attach_alphalite_factors(provider, cache: TimedCache, candidates):
    if not config.ENABLE_HISTORY_FACTORS or config.HISTORY_FACTOR_LIMIT <= 0:
        return candidates
    history_by_code = {}
    target_codes = candidates.sort_values(["pct_chg", "turnover"], ascending=False).head(
        config.HISTORY_FACTOR_LIMIT
    )["code"].tolist()
    request_fetches = 0
    max_request_fetches = max(0, int(getattr(config, "HISTORY_FACTORS_MAX_REQUEST_FETCHES", 8)))
    fetch_on_request = bool(getattr(config, "HISTORY_FACTORS_FETCH_ON_REQUEST", False))
    for code in target_codes:
        try:
            if hasattr(provider, "get_cached_history"):
                history = provider.get_cached_history(code, days=90)
            else:
                history = None
            if (history is None or history.empty) and fetch_on_request and request_fetches < max_request_fetches:
                history = provider.get_history(code, days=90)
                request_fetches += 1
        except Exception:
            continue
        if history is not None and not history.empty:
            history_by_code[code] = history
    factors = build_alphalite_factors(history_by_code)
    return merge_alphalite(candidates, factors)


def _attach_alphalite_factors_for_codes(provider, candidates, codes):
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
    history_by_code = {}
    for code in target:
        try:
            history = provider.get_history(code, days=90)
        except Exception:
            continue
        if history is not None and not history.empty:
            history_by_code[code] = history
    if not history_by_code:
        return candidates
    return merge_alphalite(candidates, build_alphalite_factors(history_by_code))


def _attach_validation_summary(
    rows: list,
    validation_store: StrategyValidationStore,
    strategy_name: str,
    days: int = 20,
    metrics_fn=None,
) -> None:
    metrics = metrics_fn(strategy_name, days) if metrics_fn else validation_store.metrics(strategy_name, days=days)
    sample_count = int(metrics.get("sample_count") or 0)
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
        "primary_horizon_label": metrics.get("primary_horizon_label"),
        "hit_3pct_rate": metrics.get("hit_3pct_rate"),
        "avg_next_close_return": metrics.get("avg_next_close_return"),
        "avg_max_drawdown_3d": metrics.get("avg_max_drawdown_3d"),
        "label": "暂无验证样本" if sample_count <= 0 else "过去同类信号",
    }
    for row in rows:
        row["similar_signal_stats"] = summary


def _market_news(provider, cache: TimedCache):
    if not config.ENABLE_MARKET_NEWS:
        return []
    cached = cache.get()
    if cached is not None:
        return cached
    try:
        market_news = provider.get_market_news(limit=80)
    except Exception:
        market_news = []
    cache.set(market_news)
    return market_news


def _strategy_status(metrics: Dict[str, object]) -> Dict[str, str]:
    return strategy_status(metrics)
