"""Lightweight diagnostic selections; importing help must not load business workers."""

SCORING_CANDIDATES = ("baseline", "prepared_reuse", "v5_industry_parallel", "combined")
PRELOAD_CONTEXT_CANDIDATES = ("baseline", "metrics_reuse")

__all__ = [
    "PRELOAD_CONTEXT_CANDIDATES",
    "SCORING_CANDIDATES",
]
