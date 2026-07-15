from __future__ import annotations

from datetime import datetime
from types import MappingProxyType
from typing import Dict, List, Mapping, Tuple

import pandas as pd

from .. import config
from ..normalization import coerce_number
from ..scoring_core import ExplanationBuilder, FeatureBuilder, RankingPolicy, RiskPolicy
from ..scoring_core import theme_limits, today_score
from .today_policies import (
    IndustryDiversificationPolicy,
    TodayBackupThresholdPolicy,
    TodayExecutionWindowPolicy,
    collect_regime_aliases,
    normalize_regime_aliases,
)


class TodayScorer:
    """Strategy object for now-time execution scoring."""

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
        self.execution_window_policy = TodayExecutionWindowPolicy()
        self.backup_threshold_policy = TodayBackupThresholdPolicy()
        self.industry_diversification_policy = IndustryDiversificationPolicy(self._industry_key)
        self._backup_threshold_plan = self.backup_threshold_policy.plan

    @staticmethod
    def _to_lower(value: object) -> str:
        return str(value or "").strip().lower()

    @staticmethod
    def _normalize_regime_aliases(regime: object) -> List[str]:
        return normalize_regime_aliases(regime)

    @staticmethod
    def _collect_aliases(value: object) -> List[str]:
        return collect_regime_aliases(value)

    def _theme_key(self, row: Dict[str, object]) -> str:
        return self._to_lower(theme_limits._tomorrow_theme_key(row))

    def _industry_key(self, row: Dict[str, object]) -> str:
        return self._to_lower(row.get("industry") or "")

    def _industry_distribution(self, rows: List[Dict[str, object]]) -> Dict[str, int]:
        return self.industry_diversification_policy.distribution(rows)

    def _apply_industry_cap(
        self,
        rows: List[Dict[str, object]],
        limit: int,
        cap: int,
    ) -> Tuple[List[Dict[str, object]], Dict[str, int], int]:
        return self.industry_diversification_policy.select(rows, limit=limit, cap=cap)

    @staticmethod
    def _parse_hhmm(value: object) -> Tuple[int, int] | None:
        if not isinstance(value, str):
            return None
        parts = value.strip().split(":")
        if len(parts) < 2:
            return None
        try:
            hour = int(parts[0])
            minute = int(parts[1])
        except ValueError:
            return None
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        return hour, minute

    @staticmethod
    def _parse_as_of(value: object) -> datetime | None:
        if isinstance(value, datetime):
            return value
        if isinstance(value, (int, float)):
            try:
                return datetime.fromtimestamp(float(value))
            except (TypeError, ValueError, OSError):
                return None
        if not isinstance(value, str):
            return None
        text = value.strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            parsed = TodayScorer._parse_hhmm(text)
            if parsed is None:
                return None
            now = datetime.now()
            return datetime(now.year, now.month, now.day, parsed[0], parsed[1])

    def _as_of(self) -> datetime:
        return (
            self._parse_as_of(self._ctx("as_of", None))
            or self._parse_as_of(self._ctx("as_of_time", None))
            or datetime.now()
        )

    def _execution_window_state(self, now: datetime) -> Tuple[bool, str, str, str]:
        return self.execution_window_policy.state(now)

    def _load_backup_threshold_plan(self) -> Dict[str, object]:
        return self.backup_threshold_policy.plan

    def _regime_key(self, market_regime: Dict[str, object]) -> str:
        return self._to_lower(
            (market_regime or {}).get("level")
            or (market_regime or {}).get("label")
            or (market_regime or {}).get("state")
            or ""
        )

    def _resolve_backup_threshold_profile(self, market_regime: Dict[str, object]) -> Dict[str, object]:
        return self.backup_threshold_policy.resolve(market_regime)

    def _rule_adjustment(self, value: float, cfg: Dict[str, object], *, use_abs: bool = False) -> float:
        if use_abs:
            value = abs(value)
        if not isinstance(cfg, dict):
            return 0.0
        value = coerce_number(value)
        adjustment = 0.0
        high_threshold = cfg.get("high_threshold")
        mid_threshold = cfg.get("mid_threshold")
        low_threshold = cfg.get("low_threshold")
        if high_threshold is not None and value >= coerce_number(high_threshold):
            adjustment += coerce_number(cfg.get("high_delta"), 0.0)
        elif mid_threshold is not None and value >= coerce_number(mid_threshold):
            adjustment += coerce_number(cfg.get("mid_delta"), 0.0)
        elif low_threshold is not None and value <= coerce_number(low_threshold):
            adjustment += coerce_number(cfg.get("low_delta"), 0.0)
        return adjustment

    def _row_backup_min_score(
        self,
        row: Dict[str, object],
        backup_profile: Dict[str, object],
        min_score: float,
    ) -> float:
        return self.backup_threshold_policy.row_min_score(row, backup_profile, min_score)

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

    def _mark_primary_row(
        self,
        row: Dict[str, object],
        min_score: float,
        executable_now: bool,
        execution_window_status: str,
        execution_window_label: str,
    ) -> None:
        row.update(
            tier="primary_watch",
            tier_label="今早重点买入",
            recommendation_class="today_term_entry",
            recommendation_class_label="今早重点买入",
            prediction_type="rank_score",
            observation_mode="today_term_entry",
            profit_window="信号时点至明日/后日规则退出",
            holding_discipline="09:30-14:00 信号窗口买入，按既定规则退出",
            execution_allowed=executable_now,
            execution_window_status=execution_window_status,
            execution_window_label=execution_window_label,
            score_note=(
                "{} 分以上形成今早执行候选；按明日/后日动态退出。".format(min_score)
                if executable_now
                else "{} 分以上形成今早执行候选，但当前窗口外，先观察。".format(min_score)
            ),
        )
        action = row.get("trade_action") if isinstance(row.get("trade_action"), dict) else {}
        if executable_now:
            action["action"] = "buy_confirmed"
            action["label"] = "可执行买入"
            action["reason"] = "今早信号满足执行阈值，按动态退出规则执行。"
            action.pop("position_size", None)
        else:
            action["action"] = "watch_only"
            action["label"] = "窗口外观察"
            action["position_size"] = 0.0
            action["reason"] = "窗口关闭后不再形成买入动作，先保留观察。"
        row["trade_action"] = action

    def _mark_backup_row(
        self,
        row: Dict[str, object],
        execution_window_status: str = "backup_only",
        execution_window_label: str = "窗口外/仅观察",
    ) -> None:
        row.update(
            tier="backup_pool",
            tier_label="今早备选观察",
            recommendation_class="today_term_backup",
            recommendation_class_label="今早备选观察",
            prediction_type="rank_score",
            holding_discipline="备选观察，仅作层级补充",
            observation_mode="today_term_backup",
            execution_allowed=False,
            execution_window_status=execution_window_status,
            execution_window_label=execution_window_label,
            score_note="未进入今早主池，仅保留备选观察。",
        )
        row["profit_window"] = "不执行"
        row["trade_action"] = {
            "action": "watch_only",
            "label": "只观察",
            "position_size": 0.0,
            "reason": "今早备选观察，不形成执行动作。",
        }
        row["prediction_type"] = "rank_score"
        row["score_note"] = "未达到执行阈值，作为备选观察输出。"

    def _empty_meta(
        self,
        top_n: int,
        market_filter: str,
        execution_allowed: bool,
        execution_window_status: str,
        execution_window_as_of: str,
    ) -> Dict[str, object]:
        return {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "candidate_count": 0,
            "top_n": top_n,
            "market_filter": market_filter,
            "strategy_version": config.TODAY_TERM_STRATEGY_VERSION,
            "strategy_label": "今早",
            "strategy": {
                "today_term": "返回可执行今早主池；不足时按备选池补齐展示。",
            },
            "primary_count": 0,
            "backup_count": 0,
            "execution_allowed": execution_allowed,
            "execution_window_status": execution_window_status,
            "execution_window_as_of": execution_window_as_of,
            "execution_window_start": str(getattr(config, "TODAY_TERM_RECOMMENDATION_BUY_WINDOW_START", "09:30")),
            "execution_window_end": str(getattr(config, "TODAY_TERM_RECOMMENDATION_BUY_WINDOW_END", "14:00")),
            "execution_window": "{}-{} 可执行窗口；窗口外仅观察".format(
                getattr(config, "TODAY_TERM_RECOMMENDATION_BUY_WINDOW_START", "09:30"),
                getattr(config, "TODAY_TERM_RECOMMENDATION_BUY_WINDOW_END", "14:00"),
            ),
            "industry_cap": getattr(config, "TODAY_MAX_INDUSTRY_PER_RECOMMENDATION", 2),
            "industry_distribution": {},
            "industry_limited_count": 0,
        }

    def _build_meta(
        self,
        candidate_count: int,
        eligible_count: int,
        display_count: int,
        primary_count: int,
        backup_count: int,
        min_score: float,
        top_n: int,
        market_filter: str,
        execution_allowed: bool,
        execution_window_status: str,
        execution_window_as_of: str,
    ) -> Dict[str, object]:
        return {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "candidate_count": candidate_count,
            "eligible_count": eligible_count,
            "display_count": display_count,
            "primary_count": primary_count,
            "backup_count": backup_count,
            "min_score": min_score,
            "backup_threshold_config": self._backup_threshold_plan,
            "backup_dynamic": self._backup_threshold_plan.get("dynamic"),
            "top_n": top_n,
            "market_filter": market_filter,
            "strategy_version": config.TODAY_TERM_STRATEGY_VERSION,
            "strategy_label": "今早",
            "recommendation_class": "today_term_entry_tiered",
            "recommendation_class_label": "今早执行候选",
            "selection_contract_version": "today_next_day_v2_tiered",
            "execution_allowed": execution_allowed,
            "execution_window_status": execution_window_status,
            "execution_window_as_of": execution_window_as_of,
            "execution_window_start": str(
                getattr(config, "TODAY_TERM_RECOMMENDATION_BUY_WINDOW_START", "09:30")
            ),
            "execution_window_end": str(
                getattr(config, "TODAY_TERM_RECOMMENDATION_BUY_WINDOW_END", "14:00")
            ),
            "holding_discipline": "信号时点至明日/后日规则退出，明日重合仅作展示层级标记。",
            "profit_window": "09:30-14:00 买入窗口，明日或后日按规则退出",
            "deepseek_mode": "precomputed_features_shadow",
            "execution_window": "{}-{} 可执行窗口；窗口外仅观察".format(
                getattr(config, "TODAY_TERM_RECOMMENDATION_BUY_WINDOW_START", "09:30"),
                getattr(config, "TODAY_TERM_RECOMMENDATION_BUY_WINDOW_END", "14:00"),
            ),
            "strategy": {
                "today_term": "返回今早可执行主池，不足按备选池补齐后展示。",
            },
            "industry_cap": getattr(config, "TODAY_MAX_INDUSTRY_PER_RECOMMENDATION", 2),
            "industry_distribution": {},
            "industry_limited_count": 0,
        }

    def _select_backup_rows(
        self,
        primary_rows: List[Dict[str, object]],
        backup_rows: List[Dict[str, object]],
        fill_n: int,
    ) -> List[Dict[str, object]]:
        if fill_n <= 0 or not backup_rows:
            return []
        primary_themes = {self._theme_key(row) for row in primary_rows if self._theme_key(row)}
        primary_industries = {self._industry_key(row) for row in primary_rows if self._industry_key(row)}

        def sort_key(row: Dict[str, object]):
            in_theme = self._theme_key(row) in primary_themes
            in_industry = self._industry_key(row) in primary_industries
            return (
                0 if in_theme else 1,
                0 if in_industry else 1,
                -coerce_number(row.get("score")),
            )

        ordered = sorted(backup_rows, key=sort_key)
        return ordered[:fill_n]

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
            execution_now = self._as_of()
            execution_open, _, _, _ = self._execution_window_state(execution_now)
            return {
                "today_term": [],
            }, self._empty_meta(
                top_n,
                market_filter,
                execution_allowed=execution_open,
                execution_window_status="immediate" if execution_open else "backup_only",
                execution_window_as_of=execution_now.isoformat(timespec="seconds"),
            )

        execution_now = self._as_of()
        execution_open, execution_window_status, _, execution_window_label = self._execution_window_state(execution_now)
        market_regime = self.feature_builder.market_regime_with_history(market_regime, df)
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
        backup_profile = self._resolve_backup_threshold_profile(market_regime)
        for row in short_rows:
            row["_selection_backup_min_score"] = self._row_backup_min_score(
                row,
                backup_profile,
                min_score,
            )
            row["selection_floor_source"] = "today_term_backup_threshold"

        primary_rows = [row for row in short_rows if coerce_number(row.get("score")) >= min_score]
        backup_rows = [
            row
            for row in short_rows
            if row not in primary_rows
            and coerce_number(row.get("score")) >= coerce_number(row.get("_selection_backup_min_score"))
        ]
        if top_n < 0:
            top_n = 0

        industry_cap = int(coerce_number(getattr(config, "TODAY_MAX_INDUSTRY_PER_RECOMMENDATION", 2), 2))
        if top_n > 0:
            ordered_rows = list(primary_rows) + list(backup_rows)
            display_rows, industry_distribution, industry_limited_count = self._apply_industry_cap(
                ordered_rows,
                top_n,
                industry_cap,
            )
        else:
            display_rows = []
            industry_distribution = {}
            industry_limited_count = 0

        self.ranking_policy.assign_rank(display_rows)

        primary_count = 0
        backup_count = 0
        for row in display_rows:
            if coerce_number(row.get("score")) >= min_score:
                primary_count += 1
                self._mark_primary_row(
                    row,
                    min_score,
                    executable_now=execution_open,
                    execution_window_status=execution_window_status,
                    execution_window_label=execution_window_label,
                )
            else:
                backup_count += 1
                self._mark_backup_row(
                    row,
                    execution_window_status="backup_only",
                    execution_window_label=execution_window_label,
                )

        meta = self._build_meta(
            len(df),
            len(primary_rows),
            len(display_rows),
            primary_count,
            backup_count,
            min_score,
            top_n=top_n,
            market_filter=market_filter,
            execution_allowed=(execution_open and primary_count > 0),
            execution_window_status=("immediate" if execution_open else "backup_only"),
            execution_window_as_of=execution_now.isoformat(timespec="seconds"),
        )
        meta["industry_distribution"] = industry_distribution
        meta["industry_limited_count"] = industry_limited_count
        meta["industry_cap"] = industry_cap
        if capture_candidate_pool:
            meta["_candidate_pool_rows"] = candidate_pool_rows
        return {"today_term": display_rows}, meta


def score_today_picks(*args, **kwargs):
    return TodayScorer().score(*args, **kwargs)
