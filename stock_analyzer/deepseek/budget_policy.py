from __future__ import annotations

from typing import Dict, List, Tuple

from ..normalization import coerce_number


class BudgetPolicy:
    """Controls which local candidates are worth sending to DeepSeek."""

    def is_executable_candidate(self, row: Dict[str, object]) -> bool:
        if not isinstance(row, dict):
            return False
        if row.get("execution_allowed") is False:
            return False
        if str(row.get("tier") or "").strip().lower() == "backup_pool":
            return False
        if str(row.get("observation_mode") or "").strip().lower() == "intraday_provisional":
            return False
        trade_action = row.get("trade_action") if isinstance(row.get("trade_action"), dict) else {}
        if trade_action:
            action = str(trade_action.get("action") or "").strip().lower()
            if action == "watch_only":
                return False
            if "position_size" in trade_action and coerce_number(trade_action.get("position_size"), 0.0) <= 0:
                return False
        return True

    def filter_executable(self, rows: List[Dict[str, object]]) -> List[Dict[str, object]]:
        return [dict(row) for row in rows or [] if self.is_executable_candidate(row)]

    def row_has_llm_edge_context(self, row: Dict[str, object]) -> bool:
        if row.get("recent_news") or row.get("announcement_flags"):
            return True
        news_sentiment = row.get("news_sentiment") if isinstance(row.get("news_sentiment"), dict) else {}
        sentiment_score = coerce_number(news_sentiment.get("score"), 50.0)
        if news_sentiment.get("risk_words") or news_sentiment.get("trigger_words"):
            return True
        if sentiment_score <= 45 or sentiment_score >= 62:
            return True
        event_risk = row.get("event_risk") if isinstance(row.get("event_risk"), dict) else {}
        blacklist_risk = row.get("blacklist_risk") if isinstance(row.get("blacklist_risk"), dict) else {}
        if event_risk.get("flags") or blacklist_risk.get("flags"):
            return True
        if row.get("event_risk_flags") or row.get("risk_words"):
            return True
        return False

    def row_is_llm_ambiguous(self, row: Dict[str, object]) -> bool:
        score = coerce_number(row.get("score"), 0.0)
        risk_penalty = coerce_number(row.get("risk_penalty"), 0.0)
        overheat_damp = coerce_number(row.get("overheat_damp"), 1.0)
        if score < 45:
            return False
        if 55 <= score <= 88:
            return True
        if risk_penalty >= 8 or overheat_damp < 0.88:
            return score <= 94
        return False

    def is_boundary_candidate(
        self,
        row: Dict[str, object],
        *,
        top_score: float = None,
        local_index: int = 0,
    ) -> bool:
        if not self.is_executable_candidate(row):
            return False
        score = coerce_number(row.get("score"), 0.0)
        if score < 45:
            return False
        risk_penalty = coerce_number(row.get("risk_penalty"), 0.0)
        overheat_damp = coerce_number(row.get("overheat_damp"), 1.0)
        execution_score = coerce_number(row.get("execution_score"), 60.0)

        if self.row_has_llm_edge_context(row) and self.row_is_llm_ambiguous(row):
            return True
        if risk_penalty >= 8 or overheat_damp < 0.9:
            return score <= 94
        if 68 <= score <= 86:
            return True
        if 52 <= execution_score <= 72 and 55 <= score <= 92:
            return True
        if top_score is not None and top_score > 0 and 0 < abs(coerce_number(top_score, 0.0) - score) <= 3.0:
            return score >= 55
        if local_index and 1 < local_index <= 3 and top_score is not None and score >= 60:
            return abs(coerce_number(top_score, 0.0) - score) <= 8.0
        return False

    def select_boundary_samples(self, rows: List[Dict[str, object]], limit: int) -> List[Dict[str, object]]:
        executable = self.filter_executable(rows)
        if not executable:
            return []
        scores = [coerce_number(row.get("score"), 0.0) for row in executable]
        top_score = max(scores) if scores else 0.0
        selected = [
            row
            for index, row in enumerate(executable, start=1)
            if self.is_boundary_candidate(row, top_score=top_score, local_index=index)
        ]
        return selected[: max(0, int(limit or 0))]

    def select_single_review_pool(
        self,
        rows: List[Dict[str, object]],
        review_limit: int,
        config: Dict[str, object] = None,
        model_tier: str = "base",
    ) -> Tuple[List[Dict[str, object]], Dict[str, object]]:
        top_rows = self._top_valid_rows(rows, review_limit)
        executable = self.filter_executable(top_rows)
        if str(model_tier or "").lower() == "pro":
            selected = self.select_boundary_samples(executable, review_limit)
            return selected, self._selection_meta(
                "pro_boundary",
                top_rows,
                executable,
                selected,
                boundary_count=len(selected),
            )
        selected = executable[: max(0, int(review_limit or 0))]
        return selected, self._selection_meta("base_executable", top_rows, executable, selected)

    def select_batch_review_pool(
        self,
        rows: List[Dict[str, object]],
        batch_limit: int,
        config: Dict[str, object],
        model_tier: str = "base",
    ) -> Tuple[List[Dict[str, object]], Dict[str, object]]:
        top_rows = self._top_valid_rows(rows, batch_limit)
        executable = self.filter_executable(top_rows)
        if str(model_tier or "").lower() == "pro":
            selected = self.select_boundary_samples(executable, batch_limit)
            return selected, self._selection_meta(
                "pro_boundary",
                top_rows,
                executable,
                selected,
                boundary_count=len(selected),
                max_review=min(len(executable), int(batch_limit or 0)),
            )
        if not executable or not (config or {}).get("cascade_filter_enabled", True):
            return executable, {
                "enabled": False,
                "mode": "base_executable",
                "input_count": len(top_rows),
                "executable_count": len(executable),
                "dropped_non_executable": max(0, len(top_rows) - len(executable)),
                "selected_count": len(executable),
            }
        max_review = min(len(executable), int((config or {}).get("cascade_max_review") or 8), int(batch_limit or 0))
        selected = [
            row
            for row in executable
            if self.row_has_llm_edge_context(row) and self.row_is_llm_ambiguous(row)
        ][:max_review]
        skipped = max(0, len(top_rows) - len(selected))
        return selected, {
            "enabled": True,
            "mode": "cascade_edge_ambiguous",
            "input_count": len(top_rows),
            "executable_count": len(executable),
            "dropped_non_executable": max(0, len(top_rows) - len(executable)),
            "selected_count": len(selected),
            "skipped_local_confident": skipped,
            "max_review": max_review,
        }

    def needs_pro_review(self, rows: List[Dict[str, object]]) -> bool:
        return bool(self.select_boundary_samples(self._top_valid_rows(rows, 3), 3))

    def _top_valid_rows(self, rows: List[Dict[str, object]], limit: int) -> List[Dict[str, object]]:
        limit = max(0, int(limit or 0))
        return [dict(row) for row in (rows or [])[: min(limit, len(rows or []))] if isinstance(row, dict)]

    def _selection_meta(
        self,
        mode: str,
        top_rows: List[Dict[str, object]],
        executable: List[Dict[str, object]],
        selected: List[Dict[str, object]],
        **extra,
    ) -> Dict[str, object]:
        meta = {
            "enabled": True,
            "mode": mode,
            "input_count": len(top_rows),
            "executable_count": len(executable),
            "dropped_non_executable": max(0, len(top_rows) - len(executable)),
            "selected_count": len(selected),
            "skipped_local_confident": max(0, len(top_rows) - len(selected)),
        }
        meta.update(extra)
        return meta
