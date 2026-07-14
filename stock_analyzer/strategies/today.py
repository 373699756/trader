from __future__ import annotations

from datetime import datetime
from types import MappingProxyType
from typing import Dict, List, Mapping, Tuple

import pandas as pd

from .. import config
from ..normalization import coerce_number
from ..scoring_core import ExplanationBuilder, FeatureBuilder, RankingPolicy, RiskPolicy
from ..scoring_core import today_score


class TodayScorer:
    """Strategy object for intraday observation scoring."""

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

    def _build_candidate_row(
        self,
        row: pd.Series,
        hot_ranks: Dict[str, int],
        industry_strength: Dict[str, float],
        sentiment_lookup: Dict[str, Dict[str, object]],
        context: Dict[str, List[float]],
        market_regime: Dict[str, object],
    ) -> Dict[str, object]:
        item = today_score._score_row(
            row,
            hot_ranks=hot_ranks,
            industry_strength=industry_strength,
            sentiment_lookup=sentiment_lookup,
            context=context,
            horizon="short",
            market_regime=market_regime,
        )
        return item

    def _mark_display_row(self, row: Dict[str, object]) -> None:
        self.risk_policy.mark_backup_watch(row, label="今日延续推荐", reason="目标是信号时点至当日收盘继续上涨；不模拟当日新买后卖出")
        row.update(observation_mode="remaining_session_continuation", recommendation_class="today_continuation", recommendation_class_label="今日延续推荐", profit_window="信号时点至T日收盘", execution_allowed=False)

    def _empty_meta(self, top_n: int, market_filter: str) -> Dict[str, object]:
        return {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "candidate_count": 0,
            "top_n": top_n,
            "market_filter": market_filter,
            "strategy_version": config.SHORT_TERM_STRATEGY_VERSION,
            "strategy_label": "今日延续推荐",
        }

    def _build_meta(
        self,
        candidate_count: int,
        eligible_count: int,
        display_count: int,
        min_score: float,
        top_n: int,
        market_filter: str,
    ) -> Dict[str, object]:
        return {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "candidate_count": candidate_count,
            "eligible_count": eligible_count,
            "display_count": display_count,
            "min_score": min_score,
            "top_n": top_n,
            "market_filter": market_filter,
            "strategy_version": config.SHORT_TERM_STRATEGY_VERSION,
            "strategy_label": "今日与明日延续推荐",
            "recommendation_class": "today_next_day_continuation_v2",
            "recommendation_class_label": "今日上涨且明日延续",
            "selection_contract_version": "today_next_day_v2",
            "execution_allowed": False,
            "holding_discipline": "信号时点至T日收盘延续收益，并要求明日策略同步确认；不模拟当日新建仓交易",
            "profit_window": "信号时点至T日收盘，并验证T+1延续",
            "deepseek_mode": "precomputed_features_shadow",
            "strategy": {
                "short_term": "双门槛推荐：预测信号后继续上涨到收盘，且明日策略同步确认",
            },
        }

    def score(
        self,
        df: pd.DataFrame,
        hot_ranks: Dict[str, int],
        industry_strength: Dict[str, float],
        sentiment_lookup: Dict[str, Dict[str, object]],
        top_n: int = 10,
        market_filter: str = "all",
        market_regime: Dict[str, object] = None,
        capture_candidate_pool: bool = False,
        scoring_context: Mapping[str, object] = None,
    ) -> Tuple[Dict[str, List[Dict[str, object]]], Dict[str, object]]:
        if scoring_context is not None:
            self.scoring_context = MappingProxyType(dict(scoring_context))
        if market_filter in ("main", "chinext", "star"):
            df = df[df["market"] == market_filter].copy()
        if df.empty:
            return {"short_term": []}, self._empty_meta(top_n, market_filter)

        context = self.feature_builder.score_context(df, industry_strength)
        short_rows: List[Dict[str, object]] = []
        for _, row in df.iterrows():
            short_rows.append(
                self._build_candidate_row(
                    row,
                    hot_ranks,
                    industry_strength,
                    sentiment_lookup,
                    context,
                    market_regime,
                )
            )

        self.ranking_policy.score_desc(short_rows)
        candidate_pool_rows = []
        for frozen_rank, row in enumerate(short_rows, start=1):
            item = dict(row)
            item["rank"] = frozen_rank
            item["frozen_rule_rank"] = frozen_rank
            candidate_pool_rows.append(item)
        min_score = coerce_number(getattr(config, "TODAY_RECOMMENDATION_MIN_SCORE", 60.0), 60.0)
        eligible_rows = [row for row in short_rows if coerce_number(row.get("score")) >= min_score]
        display_rows = eligible_rows[:top_n]
        self.ranking_policy.assign_rank(display_rows)
        for row in display_rows:
            self._mark_display_row(row)

        meta = self._build_meta(
            len(df),
            len(eligible_rows),
            len(display_rows),
            min_score,
            top_n,
            market_filter,
        )
        if capture_candidate_pool:
            meta["_candidate_pool_rows"] = candidate_pool_rows
        return {"short_term": display_rows}, meta


def score_today_picks(*args, **kwargs):
    return TodayScorer().score(*args, **kwargs)
