"""Typed keyword contracts for DeepSeek cache identity construction."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TypedDict

from trader.recommendation.domain.publication.models import Strategy


class ReviewCacheRequiredOptions(TypedDict):
    model: str


class ReviewCacheOptionalOptions(TypedDict, total=False):
    model_role: str
    thinking_mode: str
    reasoning_effort: str | None


class ReviewCacheOptions(ReviewCacheRequiredOptions, ReviewCacheOptionalOptions):
    pass


class StrategyCacheOptions(TypedDict):
    strategy: Strategy
    strategy_version: str
    dimension_weights: Mapping[str, float]
    confidence_coverage_min: float
    minimum_known_dimensions: int


__all__ = ["ReviewCacheOptions", "StrategyCacheOptions"]
