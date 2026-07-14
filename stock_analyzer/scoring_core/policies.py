from __future__ import annotations

from typing import Dict, Iterable, List, Tuple

import pandas as pd

from . import expected_return, explanations, risk, scoring_math, theme_limits, tomorrow_policy


class RiskPolicy:
    """Risk gates and penalties shared by concrete strategy scorers."""

    def sum_penalty(self, parts: Dict[str, float]) -> float:
        return risk._sum_penalty(parts)

    def tomorrow_hard_reject(self, row: pd.Series, intraday_relaxed: bool = False) -> bool:
        return tomorrow_policy._tomorrow_hard_reject(row, intraday_relaxed=intraday_relaxed)

    def tomorrow_risk_penalty_parts(self, row: pd.Series, provisional: bool = False) -> Dict[str, float]:
        return risk._tomorrow_risk_penalty_parts(row, provisional=provisional)

    def swing_risk_penalty_parts(self, row: pd.Series) -> Dict[str, float]:
        return risk._swing_risk_penalty_parts(row)

    def execution_score(self, row: pd.Series) -> float:
        return scoring_math._execution_score(row)

    def not_overextended_score(self, row: pd.Series) -> float:
        return scoring_math._not_overextended_score(row)

    def mark_backup_watch(self, row: Dict[str, object], label: str = "备选观察", reason: str = "") -> None:
        explanations.mark_backup_watch(row, label=label, reason=reason)

    def mark_tomorrow_backup_watch(self, row: Dict[str, object], reason: str = "未进入重点观察池") -> None:
        explanations.mark_tomorrow_backup_watch(row, reason=reason)

    def mark_tomorrow_intraday_watch(self, row: Dict[str, object]) -> None:
        explanations._mark_tomorrow_intraday_watch(row)


class RankingPolicy:
    """Ranking, display gates, concentration limits, and score composition."""

    def score_desc(self, rows: List[Dict[str, object]]) -> List[Dict[str, object]]:
        rows.sort(key=lambda item: item["score"], reverse=True)
        return rows

    def assign_rank(self, rows: Iterable[Dict[str, object]], start: int = 1) -> None:
        for rank, row in enumerate(rows, start=start):
            row["rank"] = rank

    def combine_details(
        self,
        components: Dict[str, float],
        strategy_name: str,
        market_regime: Dict[str, object] = None,
        row: pd.Series = None,
    ) -> Dict[str, float]:
        return scoring_math._combine_details(components, strategy_name, market_regime=market_regime, row=row)

    def attach_expected_return_prediction(
        self,
        strategy_name: str,
        rows: List[Dict[str, object]],
        samples: Iterable[Dict[str, object]] = None,
        use_ranking: bool = False,
    ) -> List[Dict[str, object]]:
        return expected_return._attach_expected_return_prediction(
            strategy_name,
            rows,
            samples=samples,
            use_ranking=use_ranking,
        )

    def tomorrow_display_gate(
        self,
        top_n: int,
        market_regime: Dict[str, object] = None,
        intraday_relaxed: bool = False,
    ) -> Tuple[int, float, str]:
        return tomorrow_policy._tomorrow_display_gate(top_n, market_regime, intraday_relaxed=intraday_relaxed)

    def limit_tomorrow_display_concentration(
        self,
        rows: List[Dict[str, object]],
        display_limit: int,
    ) -> List[Dict[str, object]]:
        return theme_limits._limit_tomorrow_display_concentration(rows, display_limit)

    def theme_count_allowed(self, counts: Dict[str, int], theme_key: str, limit: int) -> bool:
        return theme_limits._theme_count_allowed(counts, theme_key, limit)


class ExplanationBuilder:
    """Build user-facing reasons and attach rich signal explanations."""

    def attach_signal(
        self,
        item: Dict[str, object],
        row: pd.Series,
        strategy_name: str,
        strategy_label: str,
        signal_label: str,
    ) -> Dict[str, object]:
        return explanations._attach_signal_explanation(item, row, strategy_name, strategy_label, signal_label)

    def with_regime_reason(
        self,
        item: Dict[str, object],
        market_regime: Dict[str, object],
        regime_bonus: float,
    ) -> Dict[str, object]:
        return explanations._with_regime_reason(item, market_regime, regime_bonus)

    def today_reasons(self, row: pd.Series, industry_pct: float, hot_rank, sentiment: Dict[str, object]) -> List[str]:
        return explanations._build_reasons(row, industry_pct, hot_rank, sentiment)

    def tomorrow_reasons(
        self,
        row: pd.Series,
        liquidity_score: float,
        momentum_score: float,
        trend_score: float,
        historical_edge_score: float,
        execution_score: float,
        tail_setup_score: float,
        risk_penalty: float,
    ) -> List[str]:
        return explanations._build_tomorrow_reasons(
            row,
            liquidity_score,
            momentum_score,
            trend_score,
            historical_edge_score,
            execution_score,
            tail_setup_score,
            risk_penalty,
        )

    def swing_reasons(
        self,
        row: pd.Series,
        momentum_score: float,
        trend_score: float,
        liquidity_score: float,
        risk_penalty: float,
    ) -> List[str]:
        return explanations._build_swing_reasons(row, momentum_score, trend_score, liquidity_score, risk_penalty)

    def append_unique_reason(self, row: Dict[str, object], reason: str) -> None:
        explanations._append_unique_reason(row, reason)
