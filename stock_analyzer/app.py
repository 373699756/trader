from typing import Dict

from flask import Flask, jsonify, render_template, request

from . import config
from .backtest import parse_code_list, run_alphalite_backtest, run_rolling_alphalite_backtest
from .factors import build_alphalite_factors, merge_alphalite
from .providers import MarketDataProvider, TimedCache
from .scoring import (
    prepare_candidates,
    score_position_candidates,
    score_dual_horizon_candidates,
    score_swing_candidates,
    score_tech_potential_candidates,
    score_tomorrow_candidates,
)
from .sentiment import build_market_sentiment_index, score_stock_sentiment
from .strategy_validation import StrategyValidationStore
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
)


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
            strategies = []
            for item in STRATEGY_CATALOG:
                metrics = validation_store.metrics(item["name"], days=days)
                dates = validation_store.list_signal_dates(item["name"])
                latest = dates[0] if dates else {}
                strategies.append(
                    {
                        **item,
                        "metrics": metrics,
                        "latest_signal": latest,
                        "status": _strategy_status(metrics),
                    }
                )
            ranked = sorted(
                strategies,
                key=lambda row: (
                    row["metrics"].get("sample_count", 0) > 0,
                    row["metrics"].get("avg_next_close_return", -999),
                    row["metrics"].get("hit_3pct_rate", -999),
                ),
                reverse=True,
            )
            return jsonify(
                {
                    "ok": True,
                    "days": days,
                    "strategies": strategies,
                    "best_strategy": ranked[0] if ranked and ranked[0]["metrics"].get("sample_count", 0) else None,
                    "health": provider.health(),
                    "disclaimer": "仅供研究，不构成投资建议。",
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
            candidates = prepare_candidates(quotes)
            candidates = _attach_alphalite_factors(provider, factors_cache, candidates)

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
                top_n=max(top_n, 20),
                market_filter=market,
            )
            short_stability = stability_tracker.update("short_term", recommendations_by_horizon["short_term"])
            long_stability = stability_tracker.update("long_term", recommendations_by_horizon["long_term"])
            recommendations_by_horizon = {
                "short_term": short_stability["rows"][:top_n],
                "long_term": long_stability["rows"][:top_n],
            }
            _attach_validation_summary(recommendations_by_horizon["short_term"], validation_store, "short_term")
            _attach_validation_summary(recommendations_by_horizon["long_term"], validation_store, "long_term")
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
                _attach_validation_summary(saved_rows, validation_store, "tomorrow_picks")
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
        return jsonify(
            {
                "ok": True,
                "refresh_seconds": config.REFRESH_SECONDS,
                "supported_markets": config.MARKET_LABELS,
                "health": provider.health(),
            }
        )

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
            candidates = prepare_candidates(quotes)
            rows, meta = score_tomorrow_candidates(candidates, top_n=top_n, market_filter=market)
            _attach_validation_summary(rows, validation_store, "tomorrow_picks")
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
                _attach_validation_summary(saved_rows, validation_store, "tomorrow_picks")
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
            candidates = prepare_candidates(quotes)
            rows, meta = score_tech_potential_candidates(candidates, top_n=top_n, market_filter=market)
            _attach_validation_summary(rows, validation_store, "tech_potential")
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
            candidates = prepare_candidates(quotes)
            candidates = _attach_alphalite_factors(provider, factors_cache, candidates)
            rows, meta = score_swing_candidates(candidates, top_n=top_n, market_filter=market)
            _attach_validation_summary(rows, validation_store, "swing_picks")
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
            candidates = prepare_candidates(quotes)
            candidates = _attach_alphalite_factors(provider, factors_cache, candidates)
            rows, meta = score_position_candidates(candidates, top_n=top_n, market_filter=market)
            _attach_validation_summary(rows, validation_store, "position_picks")
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
        if strategy not in ("tech_potential", "tomorrow_picks", "swing_picks", "position_picks"):
            strategy = "tech_potential"
        if market not in ("all", "main", "chinext", "star"):
            market = "all"
        try:
            quotes = quotes_cache.get()
            if quotes is None:
                quotes = provider.get_realtime_quotes()
                quotes_cache.set(quotes)
            candidates = prepare_candidates(quotes)
            if strategy == "tomorrow_picks":
                rows, meta = score_tomorrow_candidates(candidates, top_n=50, market_filter=market)
                version = meta.get("strategy_version", "tomorrow_picks_v2")
            elif strategy == "swing_picks":
                candidates = _attach_alphalite_factors(provider, factors_cache, candidates)
                rows, meta = score_swing_candidates(candidates, top_n=30, market_filter=market)
                version = meta.get("strategy_version", "swing_5_10d_v1")
            elif strategy == "position_picks":
                candidates = _attach_alphalite_factors(provider, factors_cache, candidates)
                rows, meta = score_position_candidates(candidates, top_n=30, market_filter=market)
                version = meta.get("strategy_version", "position_1_3m_v1")
            else:
                rows, meta = score_tech_potential_candidates(candidates, top_n=50, market_filter=market)
                version = "tech_potential_v1"
            result = validation_store.save_signals(
                strategy,
                version,
                meta["generated_at"],
                rows,
            )
            return jsonify({"ok": True, "saved": result, "meta": meta, "health": provider.health()})
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
            return jsonify({"ok": True, "result": result, "health": provider.health()})
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

    @app.route("/api/backtest")
    def backtest():
        codes = parse_code_list(request.args.get("codes", ""))
        if not codes:
            try:
                quotes = quotes_cache.get()
                if quotes is None:
                    quotes = provider.get_realtime_quotes()
                candidates = prepare_candidates(quotes)
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
    cached = cache.get()
    if cached is not None:
        return merge_alphalite(candidates, cached)
    history_by_code = {}
    target_codes = candidates.sort_values(["pct_chg", "turnover"], ascending=False).head(
        config.HISTORY_FACTOR_LIMIT
    )["code"].tolist()
    for code in target_codes:
        try:
            history = provider.get_history(code, days=90)
        except Exception:
            continue
        if history is not None and not history.empty:
            history_by_code[code] = history
    factors = build_alphalite_factors(history_by_code)
    cache.set(factors)
    return merge_alphalite(candidates, factors)


def _attach_validation_summary(
    rows: list,
    validation_store: StrategyValidationStore,
    strategy_name: str,
    days: int = 20,
) -> None:
    metrics = validation_store.metrics(strategy_name, days=days)
    sample_count = int(metrics.get("sample_count") or 0)
    summary = {
        "strategy_name": strategy_name,
        "days": days,
        "sample_count": sample_count,
        "win_rate_next_close": metrics.get("win_rate_next_close"),
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
    sample_count = int(metrics.get("sample_count") or 0)
    avg_return = float(metrics.get("avg_next_close_return") or 0)
    hit_3pct = float(metrics.get("hit_3pct_rate") or 0)
    drawdown = float(metrics.get("avg_max_drawdown_3d") or 0)
    if sample_count < 30:
        return {"level": "pending", "label": "样本不足", "advice": "先保存并更新验证，至少积累30个样本。"}
    if avg_return > 1 and hit_3pct >= 35 and drawdown > -8:
        return {"level": "good", "label": "继续观察", "advice": "近期表现可继续跟踪，但仍需控制仓位。"}
    if avg_return < 0 or drawdown <= -10:
        return {"level": "bad", "label": "建议降权", "advice": "近期收益或回撤不理想，优先降低权重或暂停使用。"}
    return {"level": "neutral", "label": "中性", "advice": "表现不突出，继续与其他策略对比。"}
