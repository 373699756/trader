"""Immutable typed dependencies shared by DeepSeek review components."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime

from trader.domain.recommendation.models import Strategy
from trader.infra.deepseek.base_client import DeepSeekClientBase
from trader.infra.deepseek.budget import DeepSeekBudgetStore
from trader.infra.deepseek.cache import ReviewCache
from trader.infra.settings_models import DeepSeekSettings


@dataclass(frozen=True)
class ReviewerContext:
    settings: DeepSeekSettings
    budget: DeepSeekBudgetStore
    client: DeepSeekClientBase
    cache: ReviewCache
    dimension_weights: Mapping[Strategy, Mapping[str, float]]
    strategy_version: str
    confidence_coverage_min: float
    minimum_known_dimensions: int
    now: Callable[[], datetime]


__all__ = ["ReviewerContext"]
