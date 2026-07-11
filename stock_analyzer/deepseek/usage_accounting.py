from __future__ import annotations

import os
from typing import Dict, List

from ..normalization import coerce_number
from ..performance import deepseek_review_efficiency_meta


class UsageAccounting:
    """Builds token/cost hints and attaches usage metadata to reviewed rows."""

    def cost_hint(
        self,
        usage: Dict[str, object],
        model: str = "",
        model_tier: str = "",
        cached: bool = False,
        allocation_ratio: float = 1.0,
    ) -> Dict[str, object]:
        usage = usage or {}
        ratio = max(0.0, min(1.0, coerce_number(allocation_ratio, 1.0)))
        prompt_tokens = coerce_number(usage.get("prompt_tokens", usage.get("input_tokens", 0)))
        completion_tokens = coerce_number(usage.get("completion_tokens", usage.get("output_tokens", 0)))
        total_tokens = coerce_number(usage.get("total_tokens"), prompt_tokens + completion_tokens)
        if total_tokens <= 0 and prompt_tokens + completion_tokens > 0:
            total_tokens = prompt_tokens + completion_tokens
        allocated_prompt = round(prompt_tokens * ratio, 4)
        allocated_completion = round(completion_tokens * ratio, 4)
        allocated_total = round(total_tokens * ratio, 4)
        input_cost_per_1k = coerce_number(os.getenv("DEEPSEEK_INPUT_COST_PER_1K_TOKENS"), 0.0)
        output_cost_per_1k = coerce_number(os.getenv("DEEPSEEK_OUTPUT_COST_PER_1K_TOKENS"), 0.0)
        estimated_cost = round(
            allocated_prompt / 1000.0 * input_cost_per_1k
            + allocated_completion / 1000.0 * output_cost_per_1k,
            6,
        )
        return {
            "cached": bool(cached),
            "model": str(model or ""),
            "model_tier": str(model_tier or ""),
            "prompt_tokens": allocated_prompt,
            "completion_tokens": allocated_completion,
            "total_tokens": allocated_total,
            "billable_total_tokens": 0.0 if cached else allocated_total,
            "allocation_ratio": ratio,
            "estimated_cost": estimated_cost,
            "cost_unit": "env_configured",
            "has_usage": allocated_total > 0,
        }

    def efficiency_meta(
        self,
        requested_count: int,
        reviewed_count: int,
        usage_or_cost_hint: Dict[str, object],
        review_policy: Dict[str, object] = None,
    ) -> Dict[str, object]:
        execution_filtered_count = 0
        if isinstance(review_policy, dict):
            execution_filtered_count = review_policy.get("dropped_non_executable", 0)
        return deepseek_review_efficiency_meta(
            requested_count,
            reviewed_count,
            usage_or_cost_hint,
            execution_filtered_count=execution_filtered_count,
        )

    def attach_row_metadata(
        self,
        rows: List[Dict[str, object]],
        cost_hint: Dict[str, object],
        call_id: str,
        source: str,
    ) -> None:
        if not rows:
            return
        cost_hint = dict(cost_hint or {})
        usage = {
            "prompt_tokens": cost_hint.get("prompt_tokens", 0.0),
            "completion_tokens": cost_hint.get("completion_tokens", 0.0),
            "total_tokens": cost_hint.get("total_tokens", 0.0),
        }
        for row in rows:
            if not isinstance(row, dict):
                continue
            row["deepseek_call_id"] = str(call_id or "")
            row["deepseek_call_source"] = str(source or "")
            row["deepseek_usage"] = usage
            row["deepseek_cost_hint"] = cost_hint
            row["deepseek_total_tokens"] = cost_hint.get("total_tokens", 0.0)
            row["deepseek_billable_tokens"] = cost_hint.get("billable_total_tokens", 0.0)
