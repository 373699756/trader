from __future__ import annotations

from datetime import datetime
from typing import Dict, Iterable, List, Tuple

import pandas as pd

from .. import config
from ..normalization import coerce_number, percentile_score
from ..scoring_core import ExplanationBuilder, FeatureBuilder, RankingPolicy, RiskPolicy
from ..scoring_core import scoring_math, theme_limits, tomorrow_policy


class TomorrowScorer:
    """Strategy object for next-session recommendations."""

    def __init__(
        self,
        feature_builder: FeatureBuilder = None,
        risk_policy: RiskPolicy = None,
        ranking_policy: RankingPolicy = None,
        explanation_builder: ExplanationBuilder = None,
    ) -> None:
        self.feature_builder = feature_builder or FeatureBuilder()
        self.risk_policy = risk_policy or RiskPolicy()
        self.ranking_policy = ranking_policy or RankingPolicy()
        self.explanation_builder = explanation_builder or ExplanationBuilder()

    @staticmethod
    def _ranking_gate_score(row: Dict[str, object]) -> float:
        ready = str(row.get("model_confidence") or "").strip().lower() == "ready"
        expected_return_rank = str(row.get("ranking_source") or "").strip() == "expected_return_rank_score"
        if ready and expected_return_rank:
            return coerce_number(row.get("rank_score"), coerce_number(row.get("score")))
        return coerce_number(row.get("score"))

    def _build_candidate_row(
        self,
        row: pd.Series,
        context: Dict[str, List[float]],
        market_regime: Dict[str, object],
        intraday_relaxed: bool,
    ) -> Dict[str, object]:
        pct_chg = coerce_number(row.get("pct_chg"))
        volume_ratio = coerce_number(row.get("volume_ratio"))
        turnover_rate = coerce_number(row.get("turnover_rate"))
        turnover = coerce_number(row.get("turnover"))
        speed = self.feature_builder.row_speed(row)
        amplitude = coerce_number(row.get("amplitude"))
        sixty_day_pct = coerce_number(row.get("sixty_day_pct"))
        ytd_pct = coerce_number(row.get("ytd_pct"))
        ret_5d = coerce_number(row.get("ret_5d"))
        ret_10d = coerce_number(row.get("ret_10d"))
        ret_20d = coerce_number(row.get("ret_20d"))
        ma20_gap = coerce_number(row.get("ma20_gap"))
        vol_amount_5d = coerce_number(row.get("vol_amount_5d"))
        volatility_20d = coerce_number(row.get("volatility_20d"))
        breakout_20d = coerce_number(row.get("breakout_20d"))

        liquidity_score = (
            percentile_score(turnover, context["turnover_values"]) * 0.58
            + percentile_score(turnover_rate, context["turnover_rate_values"]) * 0.42
        )
        momentum_score = (
            percentile_score(pct_chg, context["pct_values"]) * 0.34
            + percentile_score(speed, context["speed_values"]) * 0.24
            + percentile_score(volume_ratio, context["volume_ratio_values"]) * 0.24
            + scoring_math._optional_factor_score(sixty_day_pct, context["sixty_day_values"]) * 0.18
        )
        trend_score = (
            percentile_score(sixty_day_pct, context["sixty_day_values"]) * 0.55
            + percentile_score(ytd_pct, context["ytd_values"]) * 0.25
            + scoring_math._optional_factor_score(
                amplitude,
                context["amplitude_values"],
                higher_is_better=False,
            ) * 0.20
        )
        execution_score = self.risk_policy.execution_score(row)
        tail_setup_score = 50.0 if intraday_relaxed else scoring_math._tail_close_setup_score(row)
        historical_edge_score = tomorrow_policy._tomorrow_historical_edge_score(row, context)
        risk_penalty_parts = self.risk_policy.tomorrow_risk_penalty_parts(row, provisional=intraday_relaxed)
        risk_penalty = self.risk_policy.sum_penalty(risk_penalty_parts)
        regime_bonus = scoring_math._market_regime_adjustment(row, market_regime, "tomorrow")
        regime_profile = scoring_math._regime_weight_profile(
            market_regime,
            ["liquidity", "momentum", "trend", "quality"],
        )
        combined = self.ranking_policy.combine_details(
            {
                "liquidity_score": liquidity_score,
                "momentum_score": momentum_score,
                "trend_score": trend_score,
                "historical_edge_score": historical_edge_score,
                "execution_score": execution_score,
                "tail_setup_score": tail_setup_score,
                "risk_penalty": risk_penalty,
                "regime_bonus": regime_bonus,
            },
            "tomorrow_picks",
            market_regime=market_regime,
            row=row,
        )
        final_score = combined["score"]
        item = {
            "code": row["code"],
            "name": str(row.get("name", "")),
            "market": row.get("market", "main"),
            "market_label": config.MARKET_LABELS.get(row.get("market", "main"), "主板"),
            "industry": str(row.get("industry", "") or ""),
            "price": round(coerce_number(row.get("price")), 3),
            "pct_chg": round(pct_chg, 2),
            "speed": round(coerce_number(row.get("speed")), 2),
            "five_min_pct": round(coerce_number(row.get("five_min_pct")), 2),
            "volume_ratio": round(volume_ratio, 2),
            "turnover_rate": round(turnover_rate, 2),
            "turnover": round(turnover, 2),
            "sixty_day_pct": round(sixty_day_pct, 2),
            "ytd_pct": round(ytd_pct, 2),
            "amplitude": round(amplitude, 2),
            "ret_5d": round(ret_5d, 2),
            "ret_10d": round(ret_10d, 2),
            "ret_20d": round(ret_20d, 2),
            "ma20_gap": round(ma20_gap, 2),
            "vol_amount_5d": round(vol_amount_5d, 2),
            "breakout_20d": bool(breakout_20d),
            "volatility_20d": round(volatility_20d, 2),
            "alphalite_factor_ready": round(coerce_number(row.get("alphalite_factor_ready")), 2),
            "alphalite_coverage": round(coerce_number(row.get("alphalite_coverage")), 2),
            "liquidity_score": round(liquidity_score, 2),
            "momentum_score": round(momentum_score, 2),
            "trend_score": round(trend_score, 2),
            "historical_edge_score": round(historical_edge_score, 2),
            "execution_score": round(execution_score, 2),
            "tail_setup_score": round(tail_setup_score, 2),
            "risk_penalty": round(risk_penalty, 2),
            "risk_penalty_parts": risk_penalty_parts,
            "mid_gain_weak_close_flag": bool(risk_penalty_parts.get("mid_gain_weak_close")),
            "regime_bonus": round(regime_bonus, 2),
            "regime_weight_profile": regime_profile,
            "base_score": round(combined["base_score"], 2),
            "raw_score": round(combined["raw_score"], 2),
            "overheat_damp": round(combined["overheat_damp"], 4),
            "score": round(max(0.0, min(100.0, final_score)), 2),
            "holding_discipline": "盘后确认候选，次日开盘入场；高开超过阈值不追",
            "profit_window": "次日",
            "reasons": self.explanation_builder.tomorrow_reasons(
                row,
                liquidity_score,
                momentum_score,
                trend_score,
                historical_edge_score,
                execution_score,
                tail_setup_score,
                risk_penalty,
            ),
        }
        item = self.risk_policy.apply_rule_penalty("tomorrow_picks", item)
        return self.explanation_builder.with_regime_reason(
            self.explanation_builder.attach_signal(
                item,
                row,
                "tomorrow_picks",
                "明日优先",
                "次日冲高",
            ),
            market_regime,
            regime_bonus,
        )

    def _select_display_rows(
        self,
        rows: List[Dict[str, object]],
        df: pd.DataFrame,
        context: Dict[str, List[float]],
        top_n: int,
        display_cap: int,
        market_regime: Dict[str, object],
        intraday_relaxed: bool,
    ) -> Dict[str, object]:
        display_limit, min_score, gate_reason = self.ranking_policy.tomorrow_display_gate(
            top_n,
            market_regime,
            intraday_relaxed=intraday_relaxed,
        )
        if display_cap is None:
            display_cap = int(coerce_number(getattr(config, "TOMORROW_RECOMMENDATION_DISPLAY_LIMIT", 8), 8))
        if int(display_cap or 0) > 0:
            display_limit = min(display_limit, int(display_cap))

        display_floor = min_score
        display_candidates = [row for row in rows if self._ranking_gate_score(row) >= display_floor]
        display_rows = self.ranking_policy.limit_tomorrow_display_concentration(display_candidates, display_limit)
        display_theme_limited_count = max(0, len(display_candidates) - len(display_rows))
        fallback_mode = ""
        backup_candidate_count = 0
        backup_min_score = coerce_number(getattr(config, "TOMORROW_BACKUP_MIN_SCORE", 45.0), 45.0)

        if not display_rows and top_n > 0:
            backup_rows = tomorrow_policy._tomorrow_backup_rows(
                df,
                context,
                market_regime=market_regime,
                provisional=intraday_relaxed,
            )
            backup_candidates = [row for row in backup_rows if row["score"] >= backup_min_score]
            backup_candidate_count = len(backup_candidates)
            display_rows = self.ranking_policy.limit_tomorrow_display_concentration(backup_candidates, display_limit)
            display_theme_limited_count = max(0, len(backup_candidates) - len(display_rows))
            if display_rows:
                fallback_mode = "backup_pool"
                display_floor = backup_min_score
                gate_reason = "{} 严格明日优先池为空，降级显示备选观察。".format(gate_reason).strip()

        if intraday_relaxed:
            gate_reason = "{} 14:30 前结果仅作盘中观察，仓位为 0，尾盘需重新确认。".format(
                gate_reason
            ).strip()

        return {
            "display_rows": display_rows,
            "display_limit": display_limit,
            "display_cap": display_cap,
            "display_floor": display_floor,
            "display_theme_limited_count": display_theme_limited_count,
            "fallback_mode": fallback_mode,
            "backup_candidate_count": backup_candidate_count,
            "backup_min_score": backup_min_score,
            "gate_reason": gate_reason,
            "min_score": min_score,
        }

    def _assign_display_tiers(
        self,
        display_rows: List[Dict[str, object]],
        min_score: float,
        market_regime: Dict[str, object],
        fallback_mode: str,
        intraday_relaxed: bool,
    ) -> Dict[str, int]:
        strict_display_count = len([row for row in display_rows if self._ranking_gate_score(row) >= min_score])
        primary_watch_n = 0 if fallback_mode or intraday_relaxed else tomorrow_policy._tomorrow_primary_watch_limit(
            strict_display_count,
            market_regime,
        )
        primary_assigned = 0
        primary_theme_counts: Dict[str, int] = {}
        theme_limited_count = 0
        ineligible_count = 0
        self.ranking_policy.assign_rank(display_rows)

        for row in display_rows:
            if intraday_relaxed:
                self.risk_policy.mark_tomorrow_intraday_watch(row)
                continue
            if fallback_mode:
                self.risk_policy.mark_tomorrow_backup_watch(row, reason="严格明日优先池为空，降级为备选观察")
                row["prediction_type"] = "rank_score"
                row["score_note"] = "综合分用于排序，不是上涨概率或预期收益率。"
                continue
            eligible, eligibility_reasons = tomorrow_policy._tomorrow_primary_eligibility(row, min_score)
            if eligibility_reasons:
                for reason in eligibility_reasons:
                    self.explanation_builder.append_unique_reason(row, reason)
            theme_key = theme_limits._tomorrow_theme_key(row)
            theme_allowed = self.ranking_policy.theme_count_allowed(
                primary_theme_counts,
                theme_key,
                getattr(config, "TOMORROW_MAX_PRIMARY_PER_THEME", 2),
            )
            if primary_watch_n > 0 and eligible and primary_assigned < primary_watch_n and theme_allowed:
                row["tier"] = "primary_watch"
                row["tier_label"] = "重点观察"
                row["execution_allowed"] = True
                row["recommendation_class"] = "next_day_priority"
                row["recommendation_class_label"] = "明日优先"
                row["profit_window"] = "次日"
                primary_assigned += 1
                primary_theme_counts[theme_key] = primary_theme_counts.get(theme_key, 0) + 1
            else:
                if not eligible:
                    ineligible_count += 1
                elif primary_watch_n <= 0:
                    self.explanation_builder.append_unique_reason(row, "盘面门控仅备选")
                elif not theme_allowed:
                    theme_limited_count += 1
                    self.explanation_builder.append_unique_reason(row, "同主题重点观察已达上限")
                self.risk_policy.mark_tomorrow_backup_watch(row)
            row["prediction_type"] = "rank_score"
            row["score_note"] = "综合分用于排序，不是上涨概率或预期收益率。"

        return {
            "primary_assigned": primary_assigned,
            "primary_watch_n": primary_watch_n,
            "theme_limited_count": theme_limited_count,
            "ineligible_count": ineligible_count,
        }

    def _empty_meta(
        self,
        top_n: int,
        market_filter: str,
        analysis_window: str,
    ) -> Dict[str, object]:
        return {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "candidate_count": 0,
            "top_n": top_n,
            "market_filter": market_filter,
            "analysis_window": analysis_window,
            "strategy_version": config.TOMORROW_STRATEGY_VERSION,
            "strategy_label": "明日优先",
            "policy": tomorrow_policy._tomorrow_policy(),
        }

    def _build_meta(
        self,
        df: pd.DataFrame,
        rows: List[Dict[str, object]],
        display_rows: List[Dict[str, object]],
        display_state: Dict[str, object],
        tier_state: Dict[str, int],
        market_regime: Dict[str, object],
        theme_distribution: Dict[str, int],
        top_n: int,
        market_filter: str,
        intraday_relaxed: bool,
        analysis_window: str,
    ) -> Dict[str, object]:
        return {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "candidate_count": len(df),
            "strict_candidate_count": len(rows),
            "screened_count": len(rows),
            "display_count": len(display_rows),
            "display_limit": display_state["display_limit"],
            "display_cap": display_state["display_cap"],
            "min_score": display_state["min_score"],
            "display_min_score": display_state["display_floor"],
            "backup_min_score": display_state["backup_min_score"],
            "backup_candidate_count": display_state["backup_candidate_count"],
            "fallback_mode": display_state["fallback_mode"],
            "primary_min_score": max(
                display_state["min_score"],
                coerce_number(getattr(config, "TOMORROW_PRIMARY_MIN_SCORE", 68.0), 68.0),
            ),
            "gate_reason": display_state["gate_reason"],
            "history_breadth20_pct": coerce_number((market_regime or {}).get("history_breadth20_pct")),
            "history_factor_coverage_pct": coerce_number((market_regime or {}).get("history_factor_coverage_pct")),
            "primary_watch_count": tier_state["primary_assigned"],
            "backup_watch_count": max(0, len(display_rows) - tier_state["primary_assigned"]),
            "primary_gate_count": tier_state["primary_watch_n"],
            "primary_ineligible_count": tier_state["ineligible_count"],
            "theme_limited_count": tier_state["theme_limited_count"],
            "display_theme_limited_count": display_state["display_theme_limited_count"],
            "theme_cap": getattr(config, "TOMORROW_MAX_PRIMARY_PER_THEME", 2),
            "display_theme_cap": getattr(config, "TOMORROW_MAX_DISPLAY_PER_THEME", 5),
            "theme_distribution": theme_distribution,
            "top_n": top_n,
            "market_filter": market_filter,
            "intraday_relaxed_mode": intraday_relaxed,
            "provisional_mode": "intraday_watch" if intraday_relaxed else "",
            "analysis_window": analysis_window,
            "strategy_version": config.TOMORROW_STRATEGY_VERSION,
            "strategy_label": "明日优先",
            "prediction_type": "rank_score",
            "score_note": "综合分是量价/趋势/风险排序分，不等于上涨概率，也不代表保证收益。",
            "holding_discipline": "盘后确认候选，次日开盘入场；高开超过阈值不追",
            "profit_window": "次日",
            "recommendation_class": "next_day_priority",
            "recommendation_class_label": "明日优先",
            "strategy": "{} 明日优先：盘后形成候选，面向次日开盘至收盘的正收益机会，优先保留成交承接、温和动能、收盘结构和买入安全的票".format(
                analysis_window,
            ),
            "policy": tomorrow_policy._tomorrow_policy(),
        }

    def score(
        self,
        df: pd.DataFrame,
        top_n: int = 50,
        market_filter: str = "all",
        market_regime: Dict[str, object] = None,
        display_cap: int = None,
        expected_return_samples: Iterable[Dict[str, object]] = None,
        use_expected_return_ranking: bool = False,
        capture_candidate_pool: bool = False,
    ) -> Tuple[List[Dict[str, object]], Dict[str, object]]:
        if market_filter in ("main", "chinext", "star"):
            df = df[df["market"] == market_filter].copy()
        analysis_window = tomorrow_policy._tomorrow_analysis_window()
        intraday_relaxed = tomorrow_policy._tomorrow_intraday_relaxed_mode(
            quote_time=tomorrow_policy._tomorrow_quote_time(df)
        )
        if df.empty:
            return [], self._empty_meta(top_n, market_filter, analysis_window)

        market_regime = self.feature_builder.market_regime_with_history(market_regime, df)
        context = self.feature_builder.score_context(df, {})
        rows: List[Dict[str, object]] = []
        for _, row in df.iterrows():
            if self.risk_policy.tomorrow_hard_reject(row, intraday_relaxed=intraday_relaxed):
                continue
            rows.append(self._build_candidate_row(row, context, market_regime, intraday_relaxed))

        self.ranking_policy.score_desc(rows)
        rows = self.ranking_policy.attach_expected_return_prediction(
            "tomorrow_picks",
            rows,
            samples=expected_return_samples,
            use_ranking=use_expected_return_ranking,
        )
        candidate_pool_rows = []
        for frozen_rank, row in enumerate(rows, start=1):
            item = dict(row)
            item["rank"] = frozen_rank
            item["frozen_rule_rank"] = frozen_rank
            candidate_pool_rows.append(item)
        display_state = self._select_display_rows(
            rows,
            df,
            context,
            top_n,
            display_cap,
            market_regime,
            intraday_relaxed,
        )
        display_rows = display_state["display_rows"]
        tier_state = self._assign_display_tiers(
            display_rows,
            display_state["min_score"],
            market_regime,
            display_state["fallback_mode"],
            intraday_relaxed,
        )
        theme_distribution = theme_limits._tomorrow_theme_distribution(display_rows)
        meta = self._build_meta(
            df,
            rows,
            display_rows,
            display_state,
            tier_state,
            market_regime,
            theme_distribution,
            top_n,
            market_filter,
            intraday_relaxed,
            analysis_window,
        )
        if capture_candidate_pool:
            meta["_candidate_pool_rows"] = candidate_pool_rows
        return display_rows, meta


def score_tomorrow_picks(*args, **kwargs):
    return TomorrowScorer().score(*args, **kwargs)
