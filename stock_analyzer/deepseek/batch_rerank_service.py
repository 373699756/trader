from __future__ import annotations

from typing import Callable, Dict, List, Tuple


class BatchRerankService:
    """Multi-strategy DeepSeek rerank orchestration."""

    def __init__(self, rerank_batch_impl: Callable) -> None:
        self._rerank_batch_impl = rerank_batch_impl

    def rerank_batch(
        self,
        rows_by_strategy: Dict[str, List[Dict[str, object]]],
        market_filter: str = "all",
        model_tier_override: str = "",
        review_limit_override: int = 0,
    ) -> Tuple[Dict[str, List[Dict[str, object]]], Dict[str, Dict[str, object]]]:
        return self._rerank_batch_impl(
            rows_by_strategy,
            market_filter=market_filter,
            model_tier_override=model_tier_override,
            review_limit_override=review_limit_override,
        )

    def rerank_candidates_batch(self, *args, **kwargs):
        return self.rerank_batch(*args, **kwargs)
