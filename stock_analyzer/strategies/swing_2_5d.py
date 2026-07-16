from __future__ import annotations

from types import MappingProxyType
from typing import Dict, Iterable, List, Mapping, Tuple

import pandas as pd

from .. import config
from ..normalization import coerce_number, finite_series, percentile_score
from ..scoring_core import ExplanationBuilder, FeatureBuilder, RankingPolicy, RiskPolicy
from ..scoring_core import scoring_math


class SwingScorer:
    """Strategy object for 2-5 day swing recommendations."""

    def __init__(
        self,
        feature_builder: FeatureBuilder = None,
        risk_policy: RiskPolicy = None,
        ranking_policy: RankingPolicy = None,
        explanation_builder: ExplanationBuilder = None,
        scoring_context: Mapping[str, object] = None,
    ) -> None:
        self.feature_builder = feature_builder or FeatureBuilder()
        self.risk_policy = risk_policy or RiskPolicy()
        self.ranking_policy = ranking_policy or RankingPolicy()
        self.explanation_builder = explanation_builder or ExplanationBuilder()
        self.scoring_context = MappingProxyType(dict(scoring_context or {}))

    def _ctx(self, name: str, default):
        return self.scoring_context.get(name, default)

    @staticmethod
    def _industry_key(row: Dict[str, object]) -> str:
        return str(row.get("industry") or "").strip().lower()

    @staticmethod
    def _industry_distribution(rows: List[Dict[str, object]]) -> Dict[str, int]:
        distribution: Dict[str, int] = {}
        for row in rows:
            key = SwingScorer._industry_key(row)
            distribution[key] = distribution.get(key, 0) + 1
        return distribution

    def _apply_industry_cap(
        self,
        rows: List[Dict[str, object]],
        limit: int,
        cap: int,
    ) -> Tuple[List[Dict[str, object]], Dict[str, int], int]:
        display_limit = max(0, int(limit or 0))
        cap_value = int(cap or 0)
        if display_limit <= 0:
            return [], {}, 0
        if cap_value <= 0:
            selected = list(rows)[:display_limit]
            return selected, self._industry_distribution(selected), 0

        selected: List[Dict[str, object]] = []
        industry_counts: Dict[str, int] = {}
        industry_limited_count = 0
        for row in rows:
            if len(selected) >= display_limit:
                break
            key = self._industry_key(row)
            if industry_counts.get(key, 0) >= cap_value:
                industry_limited_count += 1
                continue
            selected.append(row)
            industry_counts[key] = industry_counts.get(key, 0) + 1
        return selected, industry_counts, industry_limited_count

    @staticmethod
    def _ranking_gate_score(row: Dict[str, object]) -> float:
        return coerce_number(row.get("score"))

    def _build_candidate_row(
        self,
        row: pd.Series,
        context: Dict[str, List[float]],
        market_regime: Dict[str, object],
    ) -> Dict[str, object]:
        ret_5d = coerce_number(row.get("ret_5d"))
        ret_10d = coerce_number(row.get("ret_10d"))
        ret_20d = coerce_number(row.get("ret_20d"))
        ma5_gap = coerce_number(row.get("ma5_gap"))
        ma20_gap = coerce_number(row.get("ma20_gap"))
        vol_amount_5d = coerce_number(row.get("vol_amount_5d"))
        breakout_20d = coerce_number(row.get("breakout_20d"))
        volatility_20d = coerce_number(row.get("volatility_20d"))
        pct_chg = coerce_number(row.get("pct_chg"))
        turnover = coerce_number(row.get("turnover"))
        turnover_rate = coerce_number(row.get("turnover_rate"))
        volume_ratio = coerce_number(row.get("volume_ratio"))
        sixty_day_pct = coerce_number(row.get("sixty_day_pct"))
        ytd_pct = coerce_number(row.get("ytd_pct"))
        history_ready = scoring_math._historical_factors_ready(row)

        def historical_score(value, values, **kwargs):
            return scoring_math._optional_factor_score(
                value,
                values,
                available=history_ready,
                **kwargs,
            )

        momentum_score = (
            historical_score(ret_5d, context["ret_5d_values"], fallback=pct_chg, fallback_values=context["pct_values"]) * 0.24
            + historical_score(ret_10d, context["ret_10d_values"], fallback=sixty_day_pct, fallback_values=context["sixty_day_values"]) * 0.22
            + historical_score(ma5_gap, context["ma5_gap_values"], fallback=pct_chg, fallback_values=context["pct_values"]) * 0.16
            + historical_score(vol_amount_5d, context["vol_amount_5d_values"], fallback=volume_ratio, fallback_values=context["volume_ratio_values"]) * 0.18
            + percentile_score(volume_ratio, context["volume_ratio_values"]) * 0.12
            + historical_score(breakout_20d, context["breakout_20d_values"]) * 0.08
        )
        trend_score = (
            historical_score(ret_20d, context["ret_20d_values"], fallback=sixty_day_pct, fallback_values=context["sixty_day_values"]) * 0.30
            + historical_score(sixty_day_pct, context["sixty_day_values"]) * 0.26
            + historical_score(ma20_gap, context["ma20_gap_values"], fallback=sixty_day_pct, fallback_values=context["sixty_day_values"]) * 0.22
            + historical_score(ytd_pct, context["ytd_values"]) * 0.10
            + historical_score(volatility_20d, context["volatility_20d_values"], higher_is_better=False, fallback=coerce_number(row.get("amplitude")), fallback_values=context["amplitude_values"]) * 0.12
        )
        liquidity_score = (
            percentile_score(turnover, context["turnover_values"]) * 0.62
            + percentile_score(turnover_rate, context["turnover_rate_values"]) * 0.38
        )
        execution_score = self.risk_policy.execution_score(row)
        enhanced_adjustment = scoring_math._enhanced_factor_adjustment(row, context, "swing_picks")
        risk_penalty_parts = self.risk_policy.swing_risk_penalty_parts(row)
        risk_penalty = self.risk_policy.sum_penalty(risk_penalty_parts) + coerce_number(
            enhanced_adjustment.get("risk_delta")
        )
        regime_bonus = scoring_math._market_regime_adjustment(row, market_regime, "swing")
        not_overextended_score = max(
            0.0,
            min(
                100.0,
                self.risk_policy.not_overextended_score(row)
                + coerce_number(enhanced_adjustment.get("score_delta")),
            ),
        )
        regime_profile = scoring_math._regime_weight_profile(
            market_regime,
            ["momentum", "trend", "liquidity", "quality"],
        )
        combined = self.ranking_policy.combine_details(
            {
                "momentum_score": momentum_score,
                "trend_score": trend_score,
                "liquidity_score": liquidity_score,
                "execution_score": execution_score,
                "not_overextended_score": not_overextended_score,
                "risk_penalty": risk_penalty,
                "regime_bonus": regime_bonus,
            },
            "swing_picks",
            market_regime=market_regime,
            row=row,
            factor_ic_payload=context.get("factor_ic_payload"),
        )
        final_score = combined["score"]
        item = scoring_math._horizon_row(row, {
            "ret_5d": ret_5d,
            "ret_10d": ret_10d,
            "ret_20d": ret_20d,
            "ma5_gap": ma5_gap,
            "ma20_gap": ma20_gap,
            "vol_amount_5d": vol_amount_5d,
            "breakout_20d": bool(breakout_20d),
            "volatility_20d": volatility_20d,
            "momentum_score": momentum_score,
            "trend_score": trend_score,
            "liquidity_score": liquidity_score,
            "execution_score": execution_score,
            "not_overextended_score": not_overextended_score,
            "risk_penalty": risk_penalty,
            "risk_penalty_parts": risk_penalty_parts,
            "enhanced_factor_adjustment": enhanced_adjustment,
            "regime_bonus": regime_bonus,
            "regime_weight_profile": regime_profile,
            "base_score": combined["base_score"],
            "raw_score": combined["raw_score"],
            "overheat_damp": combined["overheat_damp"],
            "score": final_score,
            "horizon": "swing",
            "reasons": self.explanation_builder.swing_reasons(row, momentum_score, trend_score, liquidity_score, risk_penalty),
        })
        return self.explanation_builder.with_regime_reason(
            self.explanation_builder.attach_signal(item, row, "swing_picks", "2-5日持有", "短周期延续"),
            market_regime,
            regime_bonus,
        )

    def _mark_display_rows(
        self,
        display_rows: List[Dict[str, object]],
        factor_degraded: bool,
    ) -> None:
        self.ranking_policy.assign_rank(display_rows)
        for row in display_rows:
            if not factor_degraded:
                row["tier"] = "primary_watch"
                row["tier_label"] = "2-5日持有"
                row["execution_allowed"] = True
                row["recommendation_class"] = "hold_2_5d"
                row["recommendation_class_label"] = "2-5日持有"
                row["profit_window"] = "T日14:30后参考入场，T+2至T+5退出"
                row["holding_discipline"] = "T+1只允许保护性止损；正常止盈最早T+2，T+5强制退出"

    def _empty_meta(self, top_n: int, market_filter: str) -> Dict[str, object]:
        return scoring_math._horizon_meta(
            top_n,
            market_filter,
            0,
            config.SWING_STRATEGY_VERSION,
            "2-5日持有",
        )

    def _build_meta(
        self,
        top_n: int,
        market_filter: str,
        candidate_count: int,
        eligible_count: int,
        display_count: int,
        display_limit: int,
        min_score: float,
        history_factor_ratio: float,
        factor_degraded: bool,
        industry_distribution: Dict[str, int],
        industry_limited_count: int,
    ) -> Dict[str, object]:
        meta = scoring_math._horizon_meta(top_n, market_filter, candidate_count, config.SWING_STRATEGY_VERSION, "2-5日持有")
        meta["eligible_count"] = eligible_count
        meta["display_count"] = display_count
        meta["display_limit"] = display_limit
        meta["min_score"] = min_score
        meta["history_factor_ready_ratio"] = history_factor_ratio
        meta["factor_degraded"] = factor_degraded
        meta["primary_watch_count"] = 0 if factor_degraded else display_count
        meta["backup_watch_count"] = display_count if factor_degraded else 0
        meta["recommendation_class"] = "hold_2_5d"
        meta["recommendation_class_label"] = "2-5日持有"
        meta["profit_window"] = "T日14:30后参考入场，T+2至T+5退出"
        meta["holding_discipline"] = "T+1只允许保护性止损；正常止盈最早T+2，T+5强制退出"
        meta["deepseek_mode"] = "production_75_local_25_deepseek"
        if factor_degraded:
            meta["degraded_reason"] = "历史因子覆盖不足，2-5天趋势延续因子降级；仅供观察。"
        meta["industry_cap"] = getattr(config, "SWING_MAX_INDUSTRY_PER_RECOMMENDATION", 2)
        meta["industry_distribution"] = industry_distribution
        meta["industry_limited_count"] = industry_limited_count
        meta["strategy"] = "2-5日策略：T日14:30后参考入场，T+1保护性止损，T+2至T+5兑现收益"
        return meta

    def score(
        self,
        df: pd.DataFrame,
        top_n: int = 30,
        market_filter: str = "all",
        market_regime: Dict[str, object] = None,
        expected_return_samples: Iterable[Dict[str, object]] = None,
        use_expected_return_ranking: bool = False,
        capture_candidate_pool: bool = False,
        scoring_context: Mapping[str, object] = None,
    ) -> Tuple[List[Dict[str, object]], Dict[str, object]]:
        if scoring_context is not None:
            self.scoring_context = MappingProxyType(dict(scoring_context))
        if market_filter in ("main", "chinext", "star"):
            df = df[df["market"] == market_filter].copy()
        df = df[
            (finite_series(df, "pct_chg") <= 8)
            & (finite_series(df, "sixty_day_pct") <= 85)
            & (finite_series(df, "ytd_pct") <= 130)
            & (finite_series(df, "sixty_day_pct") >= -18)
        ].copy()
        if df.empty:
            return [], self._empty_meta(top_n, market_filter)

        if "alphalite_factor_ready" in df.columns:
            history_factor_ratio = round(float((finite_series(df, "alphalite_factor_ready") > 0).mean()), 4)
        else:
            history_factor_ratio = 0.0
        factor_degraded = history_factor_ratio < coerce_number(
            getattr(config, "SWING_MIN_HISTORY_FACTOR_COVERAGE", 0.30),
            0.30,
        )
        context = self.feature_builder.score_context(df, {})
        rows: List[Dict[str, object]] = []
        for _, row in df.iterrows():
            rows.append(self._build_candidate_row(row, context, market_regime))

        self.ranking_policy.score_desc(rows)
        post_score_rows = self._ctx("_post_score_rows", None)
        if callable(post_score_rows):
            rows = post_score_rows(rows)
        rows = self.ranking_policy.attach_expected_return_prediction(
            "swing_picks",
            rows,
            samples=expected_return_samples,
            use_ranking=use_expected_return_ranking,
        )
        self.ranking_policy.assign_selection_rank(rows)
        candidate_pool_rows = []
        for frozen_rank, row in enumerate(rows, start=1):
            item = dict(row)
            item["rank"] = row.get("selection_rank", frozen_rank)
            item["frozen_rule_rank"] = row.get("selection_rank", frozen_rank)
            item["display_rank"] = frozen_rank
            candidate_pool_rows.append(item)
        min_score = coerce_number(getattr(config, "SWING_RECOMMENDATION_MIN_SCORE", 60.0), 60.0)
        eligible_rows = [row for row in rows if self._ranking_gate_score(row) >= min_score]
        display_limit = int(top_n)
        industry_cap = int(coerce_number(getattr(config, "SWING_MAX_INDUSTRY_PER_RECOMMENDATION", 2), 2))
        if factor_degraded:
            display_limit = min(display_limit, int(getattr(config, "SWING_DEGRADED_DISPLAY_LIMIT", 8)))
            for row in eligible_rows[:display_limit]:
                self.explanation_builder.append_unique_reason(row, "历史因子覆盖不足，2-5天策略降级观察")
                row["factor_degraded"] = True
                self.risk_policy.mark_backup_watch(row, reason="历史因子覆盖不足，2-5日策略禁用执行")
        display_rows, industry_distribution, industry_limited_count = self._apply_industry_cap(
            eligible_rows,
            display_limit,
            industry_cap,
        )
        self._mark_display_rows(display_rows, factor_degraded)
        meta = self._build_meta(
            top_n,
            market_filter,
            len(df),
            len(eligible_rows),
            len(display_rows),
            display_limit,
            min_score,
            history_factor_ratio,
            factor_degraded,
            industry_distribution,
            industry_limited_count,
        )
        if capture_candidate_pool:
            meta["_candidate_pool_rows"] = candidate_pool_rows
        return display_rows, meta


def score_swing_2_5d_picks(*args, **kwargs):
    return SwingScorer().score(*args, **kwargs)
