"""Typed keyword contracts for DeepSeek cache identity construction."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TypedDict

from trader.domain.recommendation.models import Strategy


class ReviewCacheRequiredOptions(TypedDict):
    model: str


class ReviewCacheOptionalOptions(TypedDict, total=False):
    generation: str
    model_role: str
    thinking_mode: str
    reasoning_effort: str | None
    schema_version: str
    prompt_version: str


class ReviewCacheOptions(ReviewCacheRequiredOptions, ReviewCacheOptionalOptions):
    pass


class StrategyCacheRequiredOptions(TypedDict):
    strategy: Strategy
    strategy_version: str
    dimension_weights: Mapping[str, float]
    confidence_coverage_min: float
    minimum_known_dimensions: int


class StrategyCacheOptionalOptions(TypedDict, total=False):
    challenger_identity: str
    challenger_status: str


class StrategyCacheOptions(StrategyCacheRequiredOptions, StrategyCacheOptionalOptions):
    pass


__all__ = ["ReviewCacheOptions", "StrategyCacheOptions"]
