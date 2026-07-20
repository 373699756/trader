"""Typed shared state contract for DeepSeek reviewer mixins."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from trader.domain.models import Strategy
from trader.infrastructure.deepseek.base_client import DeepSeekClientBase
from trader.infrastructure.deepseek.budget import DeepSeekBudgetStore
from trader.infrastructure.deepseek.cache import ReviewCache
from trader.infrastructure.settings_models import DeepSeekSettings


class ReviewerState:
    _settings: DeepSeekSettings
    _budget: DeepSeekBudgetStore
    _client: DeepSeekClientBase
    _cache: ReviewCache
    _dimension_weights: dict[Strategy, dict[str, float]]
    _strategy_version: str
    _confidence_coverage_min: float
    _minimum_known_dimensions: int
    _now: Callable[[], datetime]
    _status_lock: Any
    _last_error: str
    _last_batch_status: str
    _last_candidate_count: int
    _last_candidate_outcomes: dict[str, int]
    _last_phase: str
    _last_strategy: str
    _last_cache_hits: int
    _last_physical_attempts: int
    _last_successful_attempts: int
    _last_failed_attempts: int
