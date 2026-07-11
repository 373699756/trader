from __future__ import annotations

from typing import Callable, Dict, List, Tuple


class RerankService:
    """Single-strategy DeepSeek rerank orchestration."""

    def __init__(self, rerank_impl: Callable) -> None:
        self._rerank_impl = rerank_impl

    def rerank(
        self,
        rows: List[Dict[str, object]],
        strategy_name: str,
        market_filter: str = "all",
        model_tier_override: str = "",
        review_limit_override: int = 0,
    ) -> Tuple[List[Dict[str, object]], Dict[str, object]]:
        return self._rerank_impl(
            rows,
            strategy_name,
            market_filter=market_filter,
            model_tier_override=model_tier_override,
            review_limit_override=review_limit_override,
        )

    def rerank_candidates(self, *args, **kwargs):
        return self.rerank(*args, **kwargs)
