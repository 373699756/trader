"""Typed scoring fixture overrides over the current strategy configuration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from typing_extensions import Unpack

from trader.download.domain.company_events import CompanyEventPolicy
from trader.recommendation.infra.settings.loading import LoadStrategySettings
from trader.recommendation.infra.settings.models import StrategySettings
from trader.recommendation.domain.candidate.filters import HardFilterPolicy
from trader.recommendation.domain.scoring.company import CompanyScoringPolicy

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class _CompanyScoringChanges(TypedDict, total=False):
    enabled: bool
    tomorrow_cap: float
    d25_cap: float
    financial_max_bonus: float
    financial_neutral: float
    financial_weights: tuple[float, float, float]
    positive_points: tuple[float, float, float]
    risk_points: tuple[float, float, float]
    tomorrow_sessions: int
    d25_sessions: int
    events: CompanyEventPolicy


class _HardFilterChanges(TypedDict, total=False):
    blacklist_codes: frozenset[str]
    structured_risk_thresholds: Mapping[str, float]


@lru_cache(maxsize=1)
def StrategySettingsFixture() -> StrategySettings:
    return LoadStrategySettings(PROJECT_ROOT / "config/strategy.json")


def CompanyScoringPolicyFixture(
    *, policy: CompanyScoringPolicy | None = None, **changes: Unpack[_CompanyScoringChanges],
) -> CompanyScoringPolicy:
    effective = policy if policy is not None else StrategySettingsFixture().company_scoring
    return replace(effective, **changes)


def HardFilterPolicyFixture(
    *, policy: HardFilterPolicy | None = None, **changes: Unpack[_HardFilterChanges],
) -> HardFilterPolicy:
    if policy is None:
        settings = StrategySettingsFixture().hard_filters
        policy = HardFilterPolicy(frozenset(settings.blacklist_codes), settings.structured_risk_thresholds)
    return replace(policy, **changes)
