from __future__ import annotations

import math
from typing import Callable, Dict, List, Tuple


class ResultMerger:
    """Merges DeepSeek records back into local ranking rows."""

    def __init__(
        self,
        *,
        normalize_code: Callable[[object], str],
        coerce_number: Callable[[object, float], float],
        clamp: Callable[[float, float, float], float],
        coerce_bool: Callable[[object], bool],
        coerce_action: Callable[[object, float, float, bool], str],
        coerce_already_priced_in: Callable[[object], bool],
        coerce_sentiment: Callable[[object], float],
        coerce_catalyst_strength: Callable[[object], object],
        coerce_time_sensitivity: Callable[[object], str],
        deepseek_event_adjustment: Callable[[str, Dict[str, object]], Dict[str, object]],
        next_day_factor_review: Callable[[Dict[str, object]], Dict[str, object]],
        rule_penalty_for_row: Callable[[str, Dict[str, object]], Tuple[float, List[object]]],
        unique_strings: Callable[[List[object]], List[str]],
        gate_decision: Callable[[Dict[str, object]], Tuple[bool, str]],
        compact_filtered_rows: Callable[[List[Dict[str, object]]], List[Dict[str, object]]],
        filter_reason_counts: Callable[[List[Dict[str, object]]], Dict[str, int]],
    ) -> None:
        self._normalize_code = normalize_code
        self._coerce_number = coerce_number
        self._clamp = clamp
        self._coerce_bool = coerce_bool
        self._coerce_action = coerce_action
        self._coerce_already_priced_in = coerce_already_priced_in
        self._coerce_sentiment = coerce_sentiment
        self._coerce_catalyst_strength = coerce_catalyst_strength
        self._coerce_time_sensitivity = coerce_time_sensitivity
        self._deepseek_event_adjustment = deepseek_event_adjustment
        self._next_day_factor_review = next_day_factor_review
        self._rule_penalty_for_row = rule_penalty_for_row
        self._unique_strings = unique_strings
        self._gate_decision = gate_decision
        self._compact_filtered_rows = compact_filtered_rows
        self._filter_reason_counts = filter_reason_counts

    def merge_ranking_rows(
        self,
        rows: List[Dict[str, object]],
        llm_records: List[Dict[str, object]],
        blend_alpha: float,
        strategy_name: str,
    ) -> Tuple[List[Dict[str, object]], Dict[str, object]]:
        llm_by_code = self._llm_by_code(llm_records)
        local_order = sorted(
            enumerate(rows),
            key=lambda pair: (
                -self._coerce_number(pair[1].get("score"), 0.0),
                int(self._coerce_number(pair[1].get("rank"), pair[0] + 1) or pair[0] + 1),
                pair[0],
            ),
        )
        local_rank_by_index = {original_index: rank for rank, (original_index, _) in enumerate(local_order, start=1)}

        merged: List[Dict[str, object]] = []
        for local_index, row in enumerate(rows, start=1):
            next_row = dict(row)
            factor_review = self._next_day_factor_review(next_row)
            code = self._normalize_code(str(next_row.get("code", "")).strip())
            base_score = self._coerce_number(next_row.get("score"), 0.0)
            if next_row.get("deepseek_rule_penalty") is not None and next_row.get("deepseek_rules_matched") is not None:
                rule_penalty = 0.0
                matched_rules = list(next_row.get("deepseek_rules_matched") or [])
            else:
                rule_penalty, matched_rules = self._rule_penalty_for_row(strategy_name, next_row)
            llm_item = llm_by_code.get(code)
            local_rank = local_rank_by_index.get(local_index - 1, local_index)
            next_row["local_rank"] = local_rank
            next_row["deepseek_covered"] = bool(llm_item)
            next_row["deepseek_blend_alpha"] = round(float(blend_alpha), 4)
            next_row["blend_alpha"] = round(float(blend_alpha), 4)
            next_row["deepseek_rule_penalty"] = self._coerce_number(next_row.get("deepseek_rule_penalty"), 0.0) + rule_penalty
            next_row["deepseek_rules_matched"] = matched_rules
            if llm_item:
                self._merge_llm_item(next_row, llm_item, factor_review, base_score, blend_alpha, rule_penalty, strategy_name)
            else:
                self._merge_local_fallback(next_row, factor_review, base_score, rule_penalty)
            self._enforce_observation_only(next_row)
            merged.append(next_row)

        gated = []
        filtered = []
        for row in merged:
            keep, reason = self._gate_decision(row)
            if keep:
                gated.append(row)
            else:
                next_row = dict(row)
                next_row["deepseek_filter_reason"] = reason
                filtered.append(next_row)

        gated.sort(key=lambda item: self._coerce_number(item.get("deepseek_rank_score"), 0.0), reverse=True)
        for rank, row in enumerate(gated, start=1):
            row["rank"] = rank
        return gated, {
            "covered": len(llm_by_code),
            "total": len(rows),
            "filtered": len(filtered),
            "filtered_codes": [row.get("code") for row in filtered[:8]],
            "filtered_rows": self._compact_filtered_rows(filtered),
            "filter_reasons": self._filter_reason_counts(filtered),
        }

    def _llm_by_code(self, llm_records: List[Dict[str, object]]) -> Dict[str, Dict[str, object]]:
        llm_by_code = {}
        for item in llm_records:
            if not isinstance(item, dict):
                continue
            code = self._normalize_code(str(item.get("code", "")).strip())
            if not code:
                continue
            llm_score = self._coerce_number(item.get("llm_score"), float("nan"))
            if math.isnan(llm_score):
                continue
            raw_flags = item.get("risk_flags", [])
            if isinstance(raw_flags, str):
                raw_flags = [raw_flags]
            risk_flags = [str(flag).strip() for flag in raw_flags if str(flag).strip()][:3] if isinstance(raw_flags, list) else []
            up_score = self._coerce_number(item.get("tomorrow_up_score"), llm_score)
            up_score = self._coerce_number(item.get("horizon_up_score"), up_score)
            penalty = self._clamp(self._coerce_number(item.get("penalty"), 0.0), 0.0, 30.0)
            penalty = self._clamp(
                penalty
                + (3.0 if self._coerce_already_priced_in(item.get("already_priced_in")) else 0.0)
                + max(0.0, -self._coerce_sentiment(item.get("sentiment"))),
                0.0,
                30.0,
            )
            veto = self._coerce_bool(item.get("veto"))
            action = self._coerce_action(item.get("action"), up_score, penalty, veto)
            if action == "avoid":
                penalty = max(penalty, 15.0)
            if veto:
                penalty = max(penalty, 30.0)
            llm_by_code[code] = {
                "llm_score": round(self._clamp(llm_score, 0.0, 100.0), 2),
                "tomorrow_up_score": round(self._clamp(up_score, 0.0, 100.0), 2),
                "action": action,
                "veto": veto,
                "penalty": round(penalty, 2),
                "reason": str(item.get("reason", "")).strip(),
                "risk_flags": risk_flags,
                "event_type": str(item.get("event_type", "")).strip(),
                "sentiment": self._coerce_sentiment(item.get("sentiment")),
                "catalyst_strength": self._coerce_catalyst_strength(item.get("catalyst_strength")),
                "time_sensitivity": self._coerce_time_sensitivity(item.get("time_sensitivity")),
                "already_priced_in": self._coerce_bool(item.get("already_priced_in")),
                "catalyst_score": round(self._clamp(self._coerce_number(item.get("catalyst_score"), 50.0), 0.0, 100.0), 2),
                "theme_truth_score": round(self._clamp(self._coerce_number(item.get("theme_truth_score"), 50.0), 0.0, 100.0), 2),
                "event_risk_score": round(self._clamp(self._coerce_number(item.get("event_risk_score"), 50.0), 0.0, 100.0), 2),
            }
        return llm_by_code

    def _merge_llm_item(
        self,
        row: Dict[str, object],
        llm_item: Dict[str, object],
        factor_review: Dict[str, object],
        base_score: float,
        blend_alpha: float,
        rule_penalty: float,
        strategy_name: str,
    ) -> None:
        llm_score = self._coerce_number(llm_item.get("llm_score"), base_score)
        up_score = self._coerce_number(llm_item.get("tomorrow_up_score"), llm_score)
        event_adjustment = self._deepseek_event_adjustment(strategy_name, llm_item)
        up_score = self._clamp(
            up_score
            + min(self._coerce_number(factor_review.get("bonus"), 0.0), 8.0) * 0.35
            + self._coerce_number(event_adjustment.get("bonus"), 0.0),
            0.0,
            100.0,
        )
        penalty = self._clamp(
            self._coerce_number(llm_item.get("penalty"), 0.0)
            + self._coerce_number(event_adjustment.get("penalty"), 0.0)
            + self._coerce_number(factor_review.get("penalty"), 0.0),
            0.0,
            45.0,
        )
        combined = round((1.0 - blend_alpha) * base_score + blend_alpha * up_score - penalty - rule_penalty, 2)
        if llm_item.get("veto") or factor_review.get("veto"):
            combined -= 50.0
        row["deepseek_score"] = llm_score
        row["tomorrow_up_score"] = up_score
        row["deepseek_horizon_score"] = up_score
        row["deepseek_action"] = llm_item.get("action") or "watch"
        if factor_review.get("veto"):
            row["deepseek_action"] = "avoid"
        row["deepseek_veto"] = bool(llm_item.get("veto") or factor_review.get("veto"))
        row["deepseek_penalty"] = penalty
        row["deepseek_reason"] = llm_item.get("reason") or ""
        row["deepseek_risk_flags"] = self._unique_strings(
            list(llm_item.get("risk_flags") or []) + list(factor_review.get("risk_flags") or [])
        )[:6]
        row["deepseek_profit_flags"] = factor_review.get("profit_flags") or []
        row["deepseek_catalyst_score"] = llm_item.get("catalyst_score")
        row["deepseek_theme_truth_score"] = llm_item.get("theme_truth_score")
        row["deepseek_event_risk_score"] = llm_item.get("event_risk_score")
        row["deepseek_event_score"] = event_adjustment.get("event_score")
        row["deepseek_event_bonus"] = event_adjustment.get("bonus")
        row["deepseek_event_penalty"] = event_adjustment.get("penalty")
        row["deepseek_event_type"] = llm_item.get("event_type") or ""
        row["deepseek_sentiment"] = llm_item.get("sentiment")
        row["deepseek_catalyst_strength"] = llm_item.get("catalyst_strength")
        row["deepseek_time_sensitivity"] = llm_item.get("time_sensitivity")
        row["deepseek_already_priced_in"] = bool(llm_item.get("already_priced_in"))
        row["deepseek_rank_score"] = combined
        row["rerank_source"] = "deepseek"

    def _merge_local_fallback(
        self,
        row: Dict[str, object],
        factor_review: Dict[str, object],
        base_score: float,
        rule_penalty: float,
    ) -> None:
        row["deepseek_score"] = None
        row["tomorrow_up_score"] = self._clamp(
            base_score + min(self._coerce_number(factor_review.get("bonus"), 0.0), 8.0) * 0.35,
            0.0,
            100.0,
        )
        row["deepseek_horizon_score"] = row["tomorrow_up_score"]
        row["deepseek_action"] = "avoid" if factor_review.get("veto") else "unknown"
        row["deepseek_veto"] = bool(factor_review.get("veto"))
        row["deepseek_penalty"] = self._coerce_number(factor_review.get("penalty"), 0.0)
        row["deepseek_reason"] = "未返回该票 LLM 打分，回退原始排序"
        row["deepseek_risk_flags"] = factor_review.get("risk_flags") or []
        row["deepseek_profit_flags"] = factor_review.get("profit_flags") or []
        row["deepseek_catalyst_score"] = None
        row["deepseek_theme_truth_score"] = None
        row["deepseek_event_risk_score"] = None
        row["deepseek_event_score"] = None
        row["deepseek_event_bonus"] = 0.0
        row["deepseek_event_penalty"] = 0.0
        row["deepseek_event_type"] = ""
        row["deepseek_sentiment"] = 0.0
        row["deepseek_catalyst_strength"] = None
        row["deepseek_time_sensitivity"] = "长期"
        row["deepseek_already_priced_in"] = False
        row["deepseek_rank_score"] = round(
            base_score - self._coerce_number(factor_review.get("penalty"), 0.0) - rule_penalty,
            2,
        )

    def _enforce_observation_only(self, row: Dict[str, object]) -> None:
        is_observation = (
            row.get("tier") == "backup_pool"
            or row.get("observation_mode") == "intraday_provisional"
            or row.get("execution_allowed") is False
        )
        if is_observation and row.get("deepseek_action") != "avoid":
            row["deepseek_action"] = "watch"
            observation_label = "盘中候选" if row.get("observation_mode") == "intraday_provisional" else "备选候选"
            row["deepseek_reason"] = "{}仅观察；{}".format(
                observation_label,
                str(row.get("deepseek_reason") or "等待14:30后确认"),
            )
