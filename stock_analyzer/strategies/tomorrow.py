from __future__ import annotations

from datetime import datetime
from types import MappingProxyType
from typing import Dict, Iterable, List, Mapping, Tuple

import pandas as pd

from .. import config
from ..normalization import coerce_number
from ..scoring_core import ExplanationBuilder, FeatureBuilder, RankingPolicy, RiskPolicy
from ..scoring_core import theme_limits, tomorrow_policy, tomorrow_score


class TomorrowScorer:
    """Strategy object for next-session recommendations."""

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
            key = TomorrowScorer._industry_key(row)
            distribution[key] = distribution.get(key, 0) + 1
        return distribution

    @staticmethod
    def _ranking_gate_score(row: Dict[str, object]) -> float:
        return coerce_number(row.get("score"))

    def _apply_display_diversity(
        self,
        rows: List[Dict[str, object]],
        limit: int,
        theme_cap: int,
        industry_cap: int,
    ) -> Tuple[List[Dict[str, object]], int, int]:
        display_limit = max(0, int(limit or 0))
        theme_limit = int(theme_cap or 0)
        industry_limit = int(industry_cap or 0)
        if display_limit <= 0:
            return [], 0, 0

        tomorrow_theme_key = self._ctx("_tomorrow_theme_key", theme_limits._tomorrow_theme_key)
        selected: List[Dict[str, object]] = []
        theme_counts: Dict[str, int] = {}
        industry_counts: Dict[str, int] = {}
        theme_limited_count = 0
        industry_limited_count = 0

        for row in rows:
            if len(selected) >= display_limit:
                break

            theme_key = tomorrow_theme_key(row)
            industry_key = self._industry_key(row)

            if theme_limit > 0 and theme_counts.get(theme_key, 0) >= theme_limit:
                theme_limited_count += 1
                continue
            if industry_limit > 0 and industry_counts.get(industry_key, 0) >= industry_limit:
                industry_limited_count += 1
                continue

            selected.append(row)
            if theme_limit > 0:
                theme_counts[theme_key] = theme_counts.get(theme_key, 0) + 1
            if industry_limit > 0:
                industry_counts[industry_key] = industry_counts.get(industry_key, 0) + 1

        return selected, theme_limited_count, industry_limited_count

    def _build_candidate_row(
        self,
        row: pd.Series,
        context: Dict[str, List[float]],
        market_regime: Dict[str, object],
        intraday_relaxed: bool,
    ) -> Dict[str, object]:
        item = tomorrow_score._tomorrow_candidate_row(
            row,
            context,
            market_regime=market_regime,
            intraday_relaxed=intraday_relaxed,
        )
        return item

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
        tomorrow_display_gate = self._ctx(
            "_tomorrow_display_gate",
            tomorrow_policy._tomorrow_display_gate,
        )
        display_limit, min_score, gate_reason = tomorrow_display_gate(
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
        display_rows, display_theme_limited_count, display_industry_limited_count = self._apply_display_diversity(
            display_candidates,
            display_limit,
            getattr(config, "TOMORROW_MAX_DISPLAY_PER_THEME", 5),
            getattr(config, "TOMORROW_MAX_INDUSTRY_PER_RECOMMENDATION", 2),
        )
        fallback_mode = ""
        backup_candidate_count = 0
        backup_min_score = coerce_number(getattr(config, "TOMORROW_BACKUP_MIN_SCORE", 45.0), 45.0)

        if not display_rows and top_n > 0:
            tomorrow_backup_rows = self._ctx(
                "_tomorrow_backup_rows",
                tomorrow_score._tomorrow_backup_rows,
            )
            backup_rows = tomorrow_backup_rows(
                df,
                context,
                market_regime=market_regime,
                provisional=intraday_relaxed,
            )
            backup_candidates = [row for row in backup_rows if row["score"] >= backup_min_score]
            backup_candidate_count = len(backup_candidates)
            display_rows, display_theme_limited_count, display_industry_limited_count = self._apply_display_diversity(
                backup_candidates,
                display_limit,
                getattr(config, "TOMORROW_MAX_DISPLAY_PER_THEME", 5),
                getattr(config, "TOMORROW_MAX_INDUSTRY_PER_RECOMMENDATION", 2),
            )
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
            "display_industry_limited_count": display_industry_limited_count,
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
        tomorrow_primary_watch_limit = self._ctx(
            "_tomorrow_primary_watch_limit",
            tomorrow_policy._tomorrow_primary_watch_limit,
        )
        primary_watch_n = 0 if fallback_mode or intraday_relaxed else tomorrow_primary_watch_limit(
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
            tomorrow_primary_eligibility = self._ctx(
                "_tomorrow_primary_eligibility",
                tomorrow_policy._tomorrow_primary_eligibility,
            )
            eligible, eligibility_reasons = tomorrow_primary_eligibility(row, min_score)
            if eligibility_reasons:
                for reason in eligibility_reasons:
                    self.explanation_builder.append_unique_reason(row, reason)
            tomorrow_theme_key = self._ctx("_tomorrow_theme_key", theme_limits._tomorrow_theme_key)
            theme_key = tomorrow_theme_key(row)
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
                if primary_watch_n > 0 and not theme_allowed:
                    theme_limited_count += 1
                    self.explanation_builder.append_unique_reason(row, "同主题重点观察已达上限")
                elif primary_watch_n <= 0:
                    self.explanation_builder.append_unique_reason(row, "盘面门控仅备选")
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
            "policy": self._ctx("_tomorrow_policy", tomorrow_policy._tomorrow_policy)(),
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
        industry_distribution: Dict[str, int],
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
            "display_industry_limited_count": display_state["display_industry_limited_count"],
            "theme_cap": getattr(config, "TOMORROW_MAX_PRIMARY_PER_THEME", 2),
            "display_theme_cap": getattr(config, "TOMORROW_MAX_DISPLAY_PER_THEME", 5),
            "industry_cap": getattr(config, "TOMORROW_MAX_INDUSTRY_PER_RECOMMENDATION", 2),
            "theme_distribution": theme_distribution,
            "industry_distribution": industry_distribution,
            "top_n": top_n,
            "market_filter": market_filter,
            "intraday_relaxed_mode": intraday_relaxed,
            "provisional_mode": "intraday_watch" if intraday_relaxed else "",
            "analysis_window": analysis_window,
            "strategy_version": config.TOMORROW_STRATEGY_VERSION,
            "strategy_label": "明日优先",
            "prediction_type": "rank_score",
            "score_note": "综合分是量价/趋势/风险排序分，不等于上涨概率，也不代表保证收益。",
            "holding_discipline": "T日14:30形成推荐，14:50冻结；验证使用14:30后信号参考价并按T+1规则退出",
            "profit_window": "T日14:30后至T+1规则退出",
            "recommendation_class": "post_1430_next_day",
            "recommendation_class_label": "明日收益",
            "deepseek_mode": "precomputed_features_shadow",
            "strategy": "{} 明日策略：T日14:30形成并在14:50冻结推荐；系统只推荐不下单".format(
                analysis_window,
            ),
            "policy": self._ctx("_tomorrow_policy", tomorrow_policy._tomorrow_policy)(),
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
        scoring_context: Mapping[str, object] = None,
    ) -> Tuple[List[Dict[str, object]], Dict[str, object]]:
        if scoring_context is not None:
            self.scoring_context = MappingProxyType(dict(scoring_context))
        if market_filter in ("main", "chinext", "star"):
            df = df[df["market"] == market_filter].copy()
        tomorrow_analysis_window = self._ctx("_tomorrow_analysis_window", tomorrow_policy._tomorrow_analysis_window)
        tomorrow_intraday_relaxed_mode = self._ctx(
            "_tomorrow_intraday_relaxed_mode",
            tomorrow_policy._tomorrow_intraday_relaxed_mode,
        )
        tomorrow_quote_time = self._ctx("_tomorrow_quote_time", tomorrow_policy._tomorrow_quote_time)
        analysis_window = tomorrow_analysis_window()
        intraday_relaxed = tomorrow_intraday_relaxed_mode(
            quote_time=tomorrow_quote_time(df)
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
        self.ranking_policy.assign_selection_rank(rows)
        candidate_pool_rows = []
        for frozen_rank, row in enumerate(rows, start=1):
            item = dict(row)
            item["rank"] = row.get("selection_rank", frozen_rank)
            item["frozen_rule_rank"] = row.get("selection_rank", frozen_rank)
            item["display_rank"] = frozen_rank
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
        theme_distribution_fn = self._ctx(
            "_tomorrow_theme_distribution",
            theme_limits._tomorrow_theme_distribution,
        )
        theme_distribution = theme_distribution_fn(display_rows)
        industry_distribution = self._industry_distribution(display_rows)
        meta = self._build_meta(
            df,
            rows,
            display_rows,
            display_state,
            tier_state,
            market_regime,
            theme_distribution,
            industry_distribution,
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
