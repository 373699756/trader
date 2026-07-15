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

    def _mark_primary_row(self, row: Dict[str, object], min_score: float) -> None:
        self.risk_policy.mark_backup_watch(row, label="今日延续重点观察", reason="信号强度满足今日延续主池阈值；仅作为观察，不触发当日建仓。")
        row.update(
            tier="primary_watch",
            tier_label="今日延续重点观察",
            recommendation_class="today_continuation_primary_watch",
            recommendation_class_label="今日延续重点观察",
            prediction_type="rank_score",
            observation_mode="today_score_primary_watch",
            profit_window="信号时点至T日收盘",
            holding_discipline="信号时点至收盘延续观察，不模拟当日新买后卖出",
            execution_allowed=False,
            score_note="{} 分以上进入今日延续强观察池；未与明日策略重合会转入备选观察。".format(min_score),
        )
        action = row.get("trade_action") if isinstance(row.get("trade_action"), dict) else {}
        action["label"] = "高优先观察"
        action["reason"] = "今日延续强观察：仅作跟踪，不形成可执行指令。"
        row["trade_action"] = action

    def _mark_backup_row(self, row: Dict[str, object]) -> None:
        self.risk_policy.mark_backup_watch(row, label="今日观察", reason="今日延续强观察池未形成可执行条件，转为今日观察")
        row["observation_mode"] = "today_score_backfill"
        row["prediction_type"] = "rank_score"
        row["score_note"] = "未进入主池时仅作观察，不形成执行信号。"

    def _empty_meta(self, top_n: int, market_filter: str) -> Dict[str, object]:
        return {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "candidate_count": 0,
            "top_n": top_n,
            "market_filter": market_filter,
            "strategy_version": config.SHORT_TERM_STRATEGY_VERSION,
            "strategy_label": "今日延续推荐",
            "strategy": {
                "short_term": "双层观察：今日延续重点观察 + 今日观察（未与明日重合时）。",
            },
            "primary_count": 0,
            "backup_count": 0,
        }

    def _build_meta(
        self,
        candidate_count: int,
        eligible_count: int,
        display_count: int,
        primary_count: int,
        backup_count: int,
        min_score: float,
        fallback_min_score: float = None,
        fallback_mode: str = "",
        fallback_count: int = 0,
        top_n: int,
        market_filter: str,
    ) -> Dict[str, object]:
        meta = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "candidate_count": candidate_count,
            "eligible_count": eligible_count,
            "display_count": display_count,
            "primary_count": primary_count,
            "backup_count": backup_count,
            "min_score": min_score,
            "top_n": top_n,
            "market_filter": market_filter,
            "strategy_version": config.SHORT_TERM_STRATEGY_VERSION,
            "strategy_label": "今日延续推荐",
            "recommendation_class": "today_next_day_continuation_tiered",
            "recommendation_class_label": "今日延续（重点观察 + 备选观察）",
            "selection_contract_version": "today_next_day_v2_tiered",
            "execution_allowed": False,
            "holding_discipline": "信号时点至T日收盘延续收益观察；明日重合仅作为优先级加权，不替代今日延续观察目标。",
            "profit_window": "信号时点至T日收盘，并记录T+1重合状态",
            "deepseek_mode": "precomputed_features_shadow",
            "strategy": {
                "short_term": "双层观察：强信号优先展示，未满足则补齐备选观察；明日未重合仅降级观察标签。",
            },
        }
        if fallback_mode:
            meta["fallback_mode"] = fallback_mode
            meta["fallback_count"] = int(fallback_count)
            if fallback_mode == "backup_min_score":
                meta["fallback_min_score"] = coerce_number(fallback_min_score)
            meta["min_score"] = min_score
        return {
            "generated_at": meta["generated_at"],
            "candidate_count": meta["candidate_count"],
            "eligible_count": meta["eligible_count"],
            "display_count": meta["display_count"],
            "primary_count": meta["primary_count"],
            "backup_count": meta["backup_count"],
            "min_score": meta["min_score"],
            "top_n": meta["top_n"],
            "market_filter": meta["market_filter"],
            "strategy_version": meta["strategy_version"],
            "strategy_label": meta["strategy_label"],
            "recommendation_class": meta["recommendation_class"],
            "recommendation_class_label": meta["recommendation_class_label"],
            "selection_contract_version": meta["selection_contract_version"],
            "execution_allowed": meta["execution_allowed"],
            "holding_discipline": meta["holding_discipline"],
            "profit_window": meta["profit_window"],
            "deepseek_mode": meta["deepseek_mode"],
            "strategy": meta["strategy"],
            **({
                "fallback_mode": meta["fallback_mode"],
                "fallback_count": meta["fallback_count"],
                "fallback_min_score": meta.get("fallback_min_score"),
            } if fallback_mode else {}),
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
        for rank, row in enumerate(short_rows, start=1):
            item = dict(row)
            item["rank"] = row.get("selection_rank", rank)
            item["frozen_rule_rank"] = row.get("selection_rank", rank)
            item["display_rank"] = rank
            candidate_pool_rows.append(item)

        min_score = coerce_number(getattr(config, "TODAY_RECOMMENDATION_MIN_SCORE", 60.0), 60.0)
        backup_min_score = coerce_number(getattr(config, "TODAY_BACKUP_MIN_SCORE", 45.0), 45.0)

        eligible_rows = [row for row in short_rows if coerce_number(row.get("score")) >= min_score]
        if top_n < 0:
            top_n = 0

        primary_rows = list(eligible_rows[:top_n]) if top_n > 0 else []
        primary_ids = {id(row) for row in primary_rows}
        display_rows = list(primary_rows)

        fallback_mode = ""
        fallback_rows = []
        remaining_slots = int(top_n) - len(display_rows) if top_n > 0 else 0

        if remaining_slots > 0:
            backup_candidates = [
                row
                for row in short_rows
                if id(row) not in primary_ids
                and coerce_number(row.get("score")) >= backup_min_score
            ]
            if backup_candidates:
                fallback_rows = backup_candidates[:remaining_slots]
                fallback_mode = "backup_min_score"
            else:
                fallback_rows = [
                    row
                    for row in short_rows
                    if id(row) not in primary_ids
                ][:remaining_slots]
                if fallback_rows:
                    fallback_mode = "rank_tail_fill"

        if fallback_rows:
            display_rows.extend(fallback_rows)

        self.ranking_policy.assign_rank(display_rows)

        primary_count = 0
        for row in display_rows:
            if id(row) in primary_ids:
                primary_count += 1
                self._mark_primary_row(row, min_score)
            else:
                self._mark_backup_row(row)

        backup_count = max(0, len(display_rows) - primary_count)
        fallback_count = len(fallback_rows)

        meta = self._build_meta(
            len(df),
            len(eligible_rows),
            len(display_rows),
            primary_count,
            backup_count,
            min_score,
            fallback_count=fallback_count,
            fallback_min_score=backup_min_score,
            fallback_mode=fallback_mode,
            top_n=top_n,
            market_filter=market_filter,
        )
        if capture_candidate_pool:
            meta["_candidate_pool_rows"] = candidate_pool_rows
        return {"short_term": display_rows}, meta


def score_today_picks(*args, **kwargs):
    return TodayScorer().score(*args, **kwargs)
