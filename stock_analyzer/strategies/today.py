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
        return self.risk_policy.apply_rule_penalty("short_term", item)

    def _mark_display_row(self, row: Dict[str, object]) -> None:
        self.risk_policy.mark_backup_watch(row, label="盘中强势观察", reason="今日策略尚无同日可执行验证，暂不形成买入指令")
        row["observation_mode"] = "intraday_strength"
        row["recommendation_class"] = "intraday_observation"
        row["recommendation_class_label"] = "盘中强势观察"
        row["profit_window"] = "仅盘中观察"

    def _empty_meta(self, top_n: int, market_filter: str) -> Dict[str, object]:
        return {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "candidate_count": 0,
            "top_n": top_n,
            "market_filter": market_filter,
            "strategy_version": config.SHORT_TERM_STRATEGY_VERSION,
            "strategy_label": "盘中强势观察",
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
            "strategy_label": "盘中强势观察",
            "recommendation_class": "intraday_observation",
            "recommendation_class_label": "盘中强势观察",
            "execution_allowed": False,
            "strategy": {
                "short_term": "盘中强势观察：涨跌幅、涨速、量比、换手、热度、舆情；同日验证补齐前仓位为0",
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
