from typing import Dict

from flask import Flask, jsonify, render_template, request

from . import config
from .backtest import parse_code_list, run_alphalite_backtest, run_rolling_alphalite_backtest
from .factors import build_alphalite_factors, merge_alphalite
from .providers import MarketDataProvider, TimedCache
from .prediction import build_stock_prediction
from .normalization import normalize_code
from .scoring import (
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
            candidates = prepare_candidates(quotes)
            market_regime = build_market_regime(candidates)
            strategies = []
            for item in STRATEGY_CATALOG:
                metrics = cached_metrics(item["name"], days)
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
            market_regime = build_market_regime(candidates)

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
        return jsonify(
            {
                "ok": True,
                "refresh_seconds": config.REFRESH_SECONDS,
                "supported_markets": config.MARKET_LABELS,
                "health": provider.health(),
            }
        )

    @app.route("/api/stock-prediction/<code>")
    def stock_prediction(code: str):
        normalized_code = code.strip()[:12]
        try:
            quotes = quotes_cache.get()
            if quotes is None:
                quotes = provider.get_realtime_quotes()
                quotes_cache.set(quotes)
            candidates = prepare_candidates(quotes)
            candidates = _attach_alphalite_factors_for_codes(provider, candidates, [normalized_code])
            market_regime = build_market_regime(candidates)
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
            )
            status = 200 if result.get("ok") else 404
            return jsonify({**result, "health": provider.health()}), status
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
            candidates = prepare_candidates(quotes)
            market_regime = build_market_regime(candidates)
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
            candidates = prepare_candidates(quotes)
            market_regime = build_market_regime(candidates)
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
            candidates = prepare_candidates(quotes)
            market_regime = build_market_regime(candidates)
            rows, meta = score_chokepoint_candidates(
                candidates,
                top_n=top_n,
                market_filter=market,
                market_regime=market_regime,
            )
            meta["market_regime"] = market_regime
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
            candidates = prepare_candidates(quotes)
            candidates = _attach_alphalite_factors(provider, factors_cache, candidates)
            market_regime = build_market_regime(candidates)
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
            candidates = prepare_candidates(quotes)
            candidates = _attach_alphalite_factors(provider, factors_cache, candidates)
            market_regime = build_market_regime(candidates)
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
            candidates = prepare_candidates(quotes)
            candidates = _attach_alphalite_factors(provider, factors_cache, candidates)
            market_regime = build_market_regime(candidates)
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
        if strategy not in ("tech_potential", "tomorrow_picks", "swing_picks", "position_picks", "chokepoint_picks", "reversal_picks", "smallcap_value_picks", "breakout_picks"):
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
                market_regime = build_market_regime(candidates)
                rows, meta = score_tomorrow_candidates(
                    candidates,
                    top_n=50,
                    market_filter=market,
                    market_regime=market_regime,
                )
                version = meta.get("strategy_version", "tomorrow_picks_v2")
            elif strategy == "swing_picks":
                candidates = _attach_alphalite_factors(provider, factors_cache, candidates)
                market_regime = build_market_regime(candidates)
                rows, meta = score_swing_candidates(
                    candidates,
                    top_n=30,
                    market_filter=market,
                    market_regime=market_regime,
                )
                version = meta.get("strategy_version", "swing_5_10d_v1")
            elif strategy == "position_picks":
                candidates = _attach_alphalite_factors(provider, factors_cache, candidates)
                market_regime = build_market_regime(candidates)
                rows, meta = score_position_candidates(
                    candidates,
                    top_n=30,
                    market_filter=market,
                    market_regime=market_regime,
                )
                version = meta.get("strategy_version", "position_1_3m_v1")
            elif strategy == "chokepoint_picks":
                market_regime = build_market_regime(candidates)
                rows, meta = score_chokepoint_candidates(
                    candidates,
                    top_n=30,
                    market_filter=market,
                    market_regime=market_regime,
                )
                version = meta.get("strategy_version", "chokepoint_v1")
            elif strategy in ("reversal_picks", "smallcap_value_picks", "breakout_picks"):
                candidates = _attach_alphalite_factors(provider, factors_cache, candidates)
                market_regime = build_market_regime(candidates)
                scorer = {
                    "reversal_picks": score_reversal_candidates,
                    "smallcap_value_picks": score_smallcap_value_candidates,
                    "breakout_picks": score_breakout_candidates,
                }[strategy]
                rows, meta = scorer(
                    candidates,
                    top_n=30,
                    market_filter=market,
                    market_regime=market_regime,
                )
                version = meta.get("strategy_version", strategy.replace("_picks", "_v1"))
            else:
                market_regime = build_market_regime(candidates)
                rows, meta = score_tech_potential_candidates(
                    candidates,
                    top_n=50,
                    market_filter=market,
                    market_regime=market_regime,
                )
                version = "tech_potential_v1"
            result = validation_store.save_signals(
                strategy,
                version,
                meta["generated_at"],
                rows,
            )
            invalidate_metrics_cache()
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
            invalidate_metrics_cache()
            return jsonify({"ok": True, "result": result, "health": provider.health()})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc), "health": provider.health()}), 502

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


def _candidate_code_rows(provider, quotes_cache: TimedCache, limit: int) -> list:
    quotes = quotes_cache.get()
    if quotes is None:
        quotes = provider.get_realtime_quotes()
        quotes_cache.set(quotes)
    candidates = prepare_candidates(quotes)
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
    sample_count = int(metrics.get("sample_count") or 0)
    real_count = int(metrics.get("real_sample_count") or 0)
    avg_return = float(metrics.get("real_avg_primary_return_net") or metrics.get("avg_primary_return_net") or 0)
    win_rate = float(metrics.get("real_win_rate_primary_net") or metrics.get("win_rate_primary_net") or 0)
    drawdown = float(metrics.get("avg_max_drawdown_3d") or 0)
    if real_count < 10 and sample_count < 30:
        return {"level": "pending", "label": "样本不足", "advice": "真实样本不足，回放只能粗筛；先保存并更新前瞻验证。"}
    if real_count < 10:
        return {"level": "pending", "label": "真实样本少", "advice": "回放样本已补足但真实样本不足，不能高权重采信。"}
    if avg_return > 0.5 and win_rate >= 52 and drawdown > -8:
        return {"level": "good", "label": "继续观察", "advice": "真实样本主周期净表现尚可，但仍需控制仓位。"}
    if avg_return < 0 or drawdown <= -10:
        return {"level": "bad", "label": "建议降权", "advice": "主周期净收益或回撤不理想，优先降低权重或暂停使用。"}
    return {"level": "neutral", "label": "中性", "advice": "表现不突出，继续与其他策略对比。"}
