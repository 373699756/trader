"""Pure point-in-time market research signal derivation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime

from trader.domain.market.factors import clamp
from trader.domain.market.models import Evidence


@dataclass(frozen=True)
class D25SignalPolicy:
    overheat_full_return_max: float
    overheat_linear_return_max: float
    overheat_linear_end_factor: float
    overheat_above_factor: float
    risk_on_breadth_min: float
    risk_off_breadth_max: float
    risk_on_factor: float
    neutral_factor: float
    risk_off_factor: float

    def __post_init__(self) -> None:
        values = (
            self.overheat_full_return_max,
            self.overheat_linear_return_max,
            self.overheat_linear_end_factor,
            self.overheat_above_factor,
            self.risk_on_breadth_min,
            self.risk_off_breadth_max,
            self.risk_on_factor,
            self.neutral_factor,
            self.risk_off_factor,
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError("d25 signal policy values must be finite")
        if self.overheat_full_return_max >= self.overheat_linear_return_max:
            raise ValueError("d25 overheat return boundaries must increase")
        if not 0.0 < self.overheat_above_factor <= self.overheat_linear_end_factor <= 1.0:
            raise ValueError("d25 overheat factors must satisfy 0 < above <= linear_end <= 1")
        if not 0.0 <= self.risk_off_breadth_max < self.risk_on_breadth_min <= 100.0:
            raise ValueError("d25 market breadth boundaries are invalid")
        if not all(0.0 < value <= 2.0 for value in self.regime_factors.values()):
            raise ValueError("d25 market regime factors must be in (0, 2]")

    @property
    def regime_factors(self) -> dict[str, float]:
        return {
            "risk_on": self.risk_on_factor,
            "neutral": self.neutral_factor,
            "risk_off": self.risk_off_factor,
        }


@dataclass(frozen=True)
class D25Signals:
    market_regime: str
    market_regime_factor: float
    overheat_factor: float
    not_overheated_score: float | None


@dataclass(frozen=True)
class LongResearchPolicy:
    financial_max_age_days: int
    announcement_lookback_days: int
    announcement_limit: int
    unlock_forward_days: int
    pe_full_score_max: float
    pe_zero_score_min: float
    pb_full_score_max: float
    pb_zero_score_min: float
    growth_points_per_pct: float
    quality_roe_neutral_pct: float
    quality_roe_points_per_pct: float
    financial_revenue_deterioration_pct: float
    financial_profit_deterioration_pct: float
    financial_core_profit_deterioration_pct: float
    pledge_thresholds: tuple[float, float, float]
    unlock_thresholds: tuple[float, float, float]
    policy_keyword_score_step: float
    negative_high_keywords: tuple[str, ...]
    negative_medium_keywords: tuple[str, ...]
    negative_low_keywords: tuple[str, ...]
    reduction_high_keywords: tuple[str, ...]
    reduction_medium_keywords: tuple[str, ...]
    reduction_low_keywords: tuple[str, ...]
    policy_positive_keywords: tuple[str, ...]
    policy_negative_keywords: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_long_numeric_policy(self)
        _validate_long_keyword_policy(self)


def _validate_long_numeric_policy(policy: LongResearchPolicy) -> None:
    if (
        min(
            policy.financial_max_age_days,
            policy.announcement_lookback_days,
            policy.announcement_limit,
            policy.unlock_forward_days,
        )
        <= 0
    ):
        raise ValueError("long research windows and limits must be positive")
    numeric = (
        policy.pe_full_score_max,
        policy.pe_zero_score_min,
        policy.pb_full_score_max,
        policy.pb_zero_score_min,
        policy.growth_points_per_pct,
        policy.quality_roe_neutral_pct,
        policy.quality_roe_points_per_pct,
        policy.financial_revenue_deterioration_pct,
        policy.financial_profit_deterioration_pct,
        policy.financial_core_profit_deterioration_pct,
        *policy.pledge_thresholds,
        *policy.unlock_thresholds,
        policy.policy_keyword_score_step,
    )
    if not all(math.isfinite(value) for value in numeric):
        raise ValueError("long research numeric policy values must be finite")
    boundaries_are_valid = (
        0.0 < policy.pe_full_score_max < policy.pe_zero_score_min
        and 0.0 < policy.pb_full_score_max < policy.pb_zero_score_min
    )
    if not boundaries_are_valid:
        raise ValueError("long PE and PB score boundaries must increase")
    if policy.growth_points_per_pct <= 0.0 or policy.quality_roe_points_per_pct <= 0.0:
        raise ValueError("long score slopes must be positive")
    if not _strictly_increasing_nonnegative(policy.pledge_thresholds):
        raise ValueError("long pledge thresholds must strictly increase")
    if not _strictly_increasing_nonnegative(policy.unlock_thresholds):
        raise ValueError("long unlock thresholds must strictly increase")
    if not 0.0 < policy.policy_keyword_score_step <= 100.0:
        raise ValueError("long policy keyword score step must be in (0, 100]")


def _validate_long_keyword_policy(policy: LongResearchPolicy) -> None:
    keyword_groups = (
        policy.negative_high_keywords,
        policy.negative_medium_keywords,
        policy.negative_low_keywords,
        policy.reduction_high_keywords,
        policy.reduction_medium_keywords,
        policy.reduction_low_keywords,
        policy.policy_positive_keywords,
        policy.policy_negative_keywords,
    )
    if any(not group or any(not keyword.strip() for keyword in group) for group in keyword_groups):
        raise ValueError("long research keyword groups must contain non-empty values")
    if any(len(group) != len(set(group)) for group in keyword_groups):
        raise ValueError("long research keyword groups must contain unique values")
    negative_levels = (
        set(policy.negative_high_keywords),
        set(policy.negative_medium_keywords),
        set(policy.negative_low_keywords),
    )
    reduction_levels = (
        set(policy.reduction_high_keywords),
        set(policy.reduction_medium_keywords),
        set(policy.reduction_low_keywords),
    )
    if _groups_overlap(negative_levels) or _groups_overlap(reduction_levels):
        raise ValueError("long research severity keyword levels must not overlap")
    if set(policy.policy_positive_keywords) & set(policy.policy_negative_keywords):
        raise ValueError("long policy positive and negative keywords must not overlap")


@dataclass(frozen=True)
class LongResearchInputs:
    price: float | None
    industry_strength: float | None
    low_volatility_score: float | None
    low_drawdown_score: float | None


@dataclass(frozen=True)
class FinancialReport:
    report_date: date
    published_at: datetime
    basic_eps: float | None = None
    book_value_per_share: float | None = None
    revenue_growth_pct: float | None = None
    net_profit_growth_pct: float | None = None
    core_profit_growth_pct: float | None = None
    roe_pct: float | None = None
    parent_net_profit: float | None = None
    core_net_profit: float | None = None

    def __post_init__(self) -> None:
        if self.published_at.tzinfo is None or self.published_at.utcoffset() is None:
            raise ValueError("financial report publication time must be timezone-aware")


@dataclass(frozen=True)
class ResearchAnnouncement:
    title: str
    published_at: datetime

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("research announcement title must not be empty")
        if self.published_at.tzinfo is None or self.published_at.utcoffset() is None:
            raise ValueError("research announcement time must be timezone-aware")


@dataclass(frozen=True)
class ResearchObservation:
    financial: FinancialReport | None = None
    announcements: tuple[ResearchAnnouncement, ...] = ()
    announcements_available: bool = False
    pledge_ratio_pct: float | None = None
    unlock_ratio_pct: float | None = None
    evidence: tuple[Evidence, ...] = ()
    source_errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.announcements and not self.announcements_available:
            raise ValueError("announcement rows require an available announcement source")


def derive_d25_signals(
    return_20d: float | None,
    market_breadth: float | None,
    policy: D25SignalPolicy,
) -> D25Signals:
    if market_breadth is None or not math.isfinite(market_breadth):
        regime = "neutral"
    elif market_breadth >= policy.risk_on_breadth_min:
        regime = "risk_on"
    elif market_breadth <= policy.risk_off_breadth_max:
        regime = "risk_off"
    else:
        regime = "neutral"

    if return_20d is None or not math.isfinite(return_20d):
        overheat_factor = 1.0
        not_overheated_score = None
    elif return_20d <= policy.overheat_full_return_max:
        overheat_factor = 1.0
        not_overheated_score = 100.0
    elif return_20d <= policy.overheat_linear_return_max:
        progress = (return_20d - policy.overheat_full_return_max) / (
            policy.overheat_linear_return_max - policy.overheat_full_return_max
        )
        overheat_factor = 1.0 - progress * (1.0 - policy.overheat_linear_end_factor)
        not_overheated_score = 100.0 * (1.0 - progress)
    else:
        overheat_factor = policy.overheat_above_factor
        not_overheated_score = 0.0
    return D25Signals(
        market_regime=regime,
        market_regime_factor=policy.regime_factors[regime],
        overheat_factor=overheat_factor,
        not_overheated_score=not_overheated_score,
    )


def derive_long_research_features(
    observation: ResearchObservation,
    inputs: LongResearchInputs,
    policy: LongResearchPolicy,
) -> dict[str, float | None]:
    event_features = _event_features(observation, policy)
    return {
        "value_score": _valuation_score(observation.financial, inputs.price, policy),
        "growth_score": _growth_score(observation.financial, policy),
        "quality_score": _quality_score(observation.financial, policy),
        "industry_policy_score": _industry_policy_score(observation, inputs.industry_strength, policy),
        "risk_protection_score": _protection_score(inputs),
        "financial_deterioration": _financial_deterioration(observation.financial, policy),
        **event_features,
    }


def _valuation_score(
    financial: FinancialReport | None,
    price: float | None,
    policy: LongResearchPolicy,
) -> float | None:
    if financial is None or (annualizer := _annualizer(financial.report_date.month)) is None:
        return None
    price_value = _finite_or_none(price)
    if price_value is None or price_value <= 0.0:
        return None
    scores: list[float] = []
    basic_eps = _finite_or_none(financial.basic_eps)
    if basic_eps is not None and basic_eps > 0.0:
        scores.append(
            _inverse_linear_score(
                price_value / (basic_eps * annualizer),
                policy.pe_full_score_max,
                policy.pe_zero_score_min,
            )
        )
    book_value = _finite_or_none(financial.book_value_per_share)
    if book_value is not None and book_value > 0.0:
        scores.append(
            _inverse_linear_score(price_value / book_value, policy.pb_full_score_max, policy.pb_zero_score_min)
        )
    return sum(scores) / len(scores) if scores else None


def _growth_values(financial: FinancialReport | None) -> tuple[float | None, float | None, float | None]:
    if financial is None:
        return None, None, None
    return (
        _finite_or_none(financial.revenue_growth_pct),
        _finite_or_none(financial.net_profit_growth_pct),
        _finite_or_none(financial.core_profit_growth_pct),
    )


def _growth_score(financial: FinancialReport | None, policy: LongResearchPolicy) -> float | None:
    known = [value for value in _growth_values(financial) if value is not None]
    return clamp(50.0 + policy.growth_points_per_pct * sum(known) / len(known)) if known else None


def _financial_deterioration(financial: FinancialReport | None, policy: LongResearchPolicy) -> float | None:
    values = _growth_values(financial)
    if not any(value is not None for value in values):
        return None
    thresholds = (
        policy.financial_revenue_deterioration_pct,
        policy.financial_profit_deterioration_pct,
        policy.financial_core_profit_deterioration_pct,
    )
    return float(
        any(value is not None and value <= threshold for value, threshold in zip(values, thresholds, strict=True))
    )


def _quality_score(financial: FinancialReport | None, policy: LongResearchPolicy) -> float | None:
    if financial is None or (annualizer := _annualizer(financial.report_date.month)) is None:
        return None
    scores: list[float] = []
    roe = _finite_or_none(financial.roe_pct)
    if roe is not None:
        scores.append(
            clamp(50.0 + (roe * annualizer - policy.quality_roe_neutral_pct) * policy.quality_roe_points_per_pct)
        )
    parent_profit = _finite_or_none(financial.parent_net_profit)
    core_profit = _finite_or_none(financial.core_net_profit)
    if parent_profit is not None and parent_profit > 0.0 and core_profit is not None:
        scores.append(clamp(core_profit / parent_profit * 100.0))
    return sum(scores) / len(scores) if scores else None


def _event_features(
    observation: ResearchObservation,
    policy: LongResearchPolicy,
) -> dict[str, float | None]:
    if not observation.announcements_available:
        reduction_level = None
        negative_level = None
    else:
        negative_level = float(
            max((_announcement_level(item.title, policy) for item in observation.announcements), default=0)
        )
        reduction_level = max((_reduction_level(item.title, policy) for item in observation.announcements), default=0)
    unlock_level = _severity(observation.unlock_ratio_pct, policy.unlock_thresholds)
    return {
        "negative_announcement_level": negative_level,
        "pledge_risk": _severity(observation.pledge_ratio_pct, policy.pledge_thresholds),
        "shareholder_reduction_level": float(reduction_level) if reduction_level is not None else None,
        "unlock_risk": unlock_level,
        "reduction_or_unlock": _combined_event_level(reduction_level, unlock_level),
    }


def _industry_policy_score(
    observation: ResearchObservation,
    industry_strength: float | None,
    policy: LongResearchPolicy,
) -> float | None:
    if not observation.announcements_available:
        return None
    titles = tuple(item.title for item in observation.announcements)
    positive_hits = sum(any(keyword in title for title in titles) for keyword in policy.policy_positive_keywords)
    negative_hits = sum(any(keyword in title for title in titles) for keyword in policy.policy_negative_keywords)
    evidence_score = clamp(50.0 + policy.policy_keyword_score_step * (positive_hits - negative_hits))
    industry_value = _finite_or_none(industry_strength)
    return None if industry_value is None else clamp(0.6 * industry_value + 0.4 * evidence_score)


def _protection_score(inputs: LongResearchInputs) -> float | None:
    volatility = _finite_or_none(inputs.low_volatility_score)
    drawdown = _finite_or_none(inputs.low_drawdown_score)
    return None if volatility is None or drawdown is None else clamp(0.5 * volatility + 0.5 * drawdown)


def announcement_level(title: str, policy: LongResearchPolicy) -> int:
    return _announcement_level(title, policy)


def reduction_level(title: str, policy: LongResearchPolicy) -> int:
    return _reduction_level(title, policy)


def _announcement_level(title: str, policy: LongResearchPolicy) -> int:
    return _keyword_level(
        title,
        high=policy.negative_high_keywords,
        medium=policy.negative_medium_keywords,
        low=policy.negative_low_keywords,
    )


def _reduction_level(title: str, policy: LongResearchPolicy) -> int:
    normalized = "".join(title.split())
    if "终止" in normalized and any(marker in normalized for marker in ("未实施", "未减持", "尚未实施")):
        return 0
    return _keyword_level(
        title,
        high=policy.reduction_high_keywords,
        medium=policy.reduction_medium_keywords,
        low=policy.reduction_low_keywords,
    )


def _keyword_level(
    title: str,
    *,
    high: tuple[str, ...],
    medium: tuple[str, ...],
    low: tuple[str, ...],
) -> int:
    if any(keyword in title for keyword in high):
        return 3
    if any(keyword in title for keyword in medium):
        return 2
    if any(keyword in title for keyword in low):
        return 1
    return 0


def _severity(value: float | None, thresholds: tuple[float, float, float]) -> float | None:
    finite = _finite_or_none(value)
    if finite is None or finite < 0.0:
        return None
    if finite >= thresholds[2]:
        return 3.0
    if finite >= thresholds[1]:
        return 2.0
    if finite >= thresholds[0]:
        return 1.0
    return 0.0


def _combined_event_level(first: int | None, second: float | None) -> float | None:
    known = [float(value) for value in (first, second) if value is not None]
    if any(value > 0.0 for value in known):
        return max(known)
    if first is not None and second is not None:
        return 0.0
    return None


def _annualizer(report_month: int) -> float | None:
    return {3: 4.0, 6: 2.0, 9: 4.0 / 3.0, 12: 1.0}.get(report_month)


def _inverse_linear_score(value: float, full_score_max: float, zero_score_min: float) -> float:
    return clamp(100.0 * (zero_score_min - value) / (zero_score_min - full_score_max))


def _finite_or_none(value: float | None) -> float | None:
    if value is None:
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _strictly_increasing_nonnegative(values: tuple[float, float, float]) -> bool:
    return 0.0 <= values[0] < values[1] < values[2]


def _groups_overlap(groups: tuple[set[str], set[str], set[str]]) -> bool:
    return bool((groups[0] & groups[1]) or (groups[0] & groups[2]) or (groups[1] & groups[2]))


__all__ = [
    "D25SignalPolicy",
    "D25Signals",
    "FinancialReport",
    "LongResearchPolicy",
    "LongResearchInputs",
    "ResearchAnnouncement",
    "ResearchObservation",
    "announcement_level",
    "derive_d25_signals",
    "derive_long_research_features",
    "reduction_level",
]
