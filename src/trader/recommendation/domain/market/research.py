"""Pure point-in-time market research signal derivation."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from types import MappingProxyType
from typing import Literal

from trader.download.domain.company_events import CompanyRiskType
from trader.recommendation.domain.market.factors import Clamp, WeightedScore
from trader.recommendation.domain.market.models import Evidence
from trader.recommendation.domain.scoring.company import CompanyScoringContext, IsCompanyRiskEffective


@dataclass(frozen=True)
class MarketRegimePolicy:
    risk_on_breadth_min: float
    risk_off_breadth_max: float

    def __post_init__(self) -> None:
        values = (self.risk_on_breadth_min, self.risk_off_breadth_max)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("market regime policy values must be finite")
        if not 0.0 <= self.risk_off_breadth_max < self.risk_on_breadth_min <= 100.0:
            raise ValueError("market breadth boundaries are invalid")


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

    def __post_init__(self) -> None:
        _validate_long_numeric_policy(self)


@dataclass(frozen=True)
class FeatureComponentWeightPolicy:
    """Weights for composite production features derived before strategy scoring."""

    trend_score: Mapping[str, float]
    industry_policy_score: Mapping[str, float]
    risk_protection_score: Mapping[str, float]

    def __post_init__(self) -> None:
        for name, configured in (
            ("trend_score", self.trend_score),
            ("industry_policy_score", self.industry_policy_score),
            ("risk_protection_score", self.risk_protection_score),
        ):
            weights = dict(configured)
            if not weights or any(not math.isfinite(value) or value < 0.0 for value in weights.values()):
                raise ValueError(f"{name} weights must be finite and nonnegative")
            if abs(sum(weights.values()) - 1.0) > 1e-9:
                raise ValueError(f"{name} weights must sum to 1.0")
            object.__setattr__(self, name, MappingProxyType(weights))


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
    announcement_id: str = ""
    source: str = "issuer_disclosure"
    body: str = ""
    body_checked: bool = False

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("research announcement title must not be empty")
        if self.published_at.tzinfo is None or self.published_at.utcoffset() is None:
            raise ValueError("research announcement time must be timezone-aware")
        if not self.source.strip():
            raise ValueError("research announcement source must not be empty")


class CorporateRiskCategory(str, Enum):
    MAJOR_SHAREHOLDER_REDUCTION = "major_shareholder_reduction"
    FINANCIAL_FRAUD = "financial_fraud_history"
    OFFICIAL_INVESTIGATION = "official_investigation_history"
    MAJOR_ILLEGAL = "major_illegal_history"
    FUND_OCCUPATION = "fund_occupation_history"
    ILLEGAL_GUARANTEE = "illegal_guarantee_history"
    FORCED_DELISTING = "forced_delisting_risk"


_OFFICIAL_CORPORATE_RISK_SOURCES = frozenset(
    {
        "issuer_disclosure",
        "exchange_disclosure",
        "regulator_disclosure",
        "judicial_disclosure",
        "csrc_disclosure",
        "eastmoney_announcement",
    }
)


@dataclass(frozen=True)
class CorporateRiskFact:
    category: CorporateRiskCategory
    announced_at: datetime
    evidence_id: str
    source: str
    resolved_at: datetime | None = None
    case_id: str = ""
    expires_at: datetime | None = None
    persistent: bool = False

    def __post_init__(self) -> None:
        if self.announced_at.tzinfo is None or self.announced_at.utcoffset() is None:
            raise ValueError("corporate risk announcement time must be timezone-aware")
        if self.resolved_at is not None:
            if self.resolved_at.tzinfo is None or self.resolved_at.utcoffset() is None:
                raise ValueError("corporate risk resolution time must be timezone-aware")
            if self.resolved_at < self.announced_at:
                raise ValueError("corporate risk resolution cannot precede announcement")
        if not self.evidence_id.strip():
            raise ValueError("corporate risk evidence id must not be empty")
        if self.source not in _OFFICIAL_CORPORATE_RISK_SOURCES:
            raise ValueError("corporate risk fact source must be an official structured disclosure")


@dataclass(frozen=True)
class ResearchObservation:
    company_context: CompanyScoringContext | None = None
    data_version: str = "research:unassigned"
    queried_on: date | None = None
    pledge_checked_on: date | None = None
    financial: FinancialReport | None = None
    financial_history: tuple[FinancialReport, ...] = ()
    financial_history_complete: bool = False
    announcements: tuple[ResearchAnnouncement, ...] = ()
    announcements_available: bool = False
    pledge_ratio_pct: float | None = None
    unlock_ratio_pct: float | None = None
    corporate_risk_facts: tuple[CorporateRiskFact, ...] = ()
    corporate_risk_window_complete: bool = False
    risk_window_start: date | None = None
    risk_window_end: date | None = None
    corporate_risk_evidence_version: str = ""
    news_queried: bool = False
    evidence: tuple[Evidence, ...] = ()
    source_errors: tuple[str, ...] = ()
    snapshot_at: datetime | None = None
    loaded_at: datetime | None = None
    unlock_window_start: date | None = None
    unlock_window_end: date | None = None

    def __post_init__(self) -> None:
        if self.snapshot_at is not None and self.snapshot_at.utcoffset() is None:
            raise ValueError("research snapshot time must be timezone-aware")
        if self.loaded_at is not None and self.loaded_at.utcoffset() is None:
            raise ValueError("research load time must be timezone-aware")
        if self.announcements and not self.announcements_available:
            raise ValueError("announcement rows require an available announcement source")
        if self.financial is not None and self.financial_history and self.financial not in self.financial_history:
            raise ValueError("current financial report must belong to financial history")
        if self.financial_history_complete and not self.financial_history:
            raise ValueError("complete financial history requires report rows")


ResearchComponentStatus = Literal["known_clear", "known_risk", "unknown", "stale"]


@dataclass(frozen=True)
class ResearchComponentCoverage:
    financial: ResearchComponentStatus
    announcements: ResearchComponentStatus
    pledge: ResearchComponentStatus
    unlock: ResearchComponentStatus

    def __post_init__(self) -> None:
        valid = {"known_clear", "known_risk", "unknown", "stale"}
        if any(value not in valid for value in (self.financial, self.announcements, self.pledge, self.unlock)):
            raise ValueError("research component status is invalid")


def ResearchComponentsFromObservation(
    observation: ResearchObservation | None, observed_at: datetime | None = None
) -> ResearchComponentCoverage:
    if observation is None:
        return ResearchComponentCoverage("unknown", "unknown", "unknown", "unknown")
    financial: ResearchComponentStatus = "known_clear" if observation.financial is not None else "unknown"
    if any(item.startswith(("financial:", "company_snapshot:")) for item in observation.source_errors):
        financial = "stale" if observation.financial is not None else "unknown"
    at = observed_at or observation.loaded_at or observation.snapshot_at
    known_risk = any(at is None or _corporate_risk_is_effective(fact, at) for fact in observation.corporate_risk_facts)
    if observation.company_context is not None and at is not None:
        known_risk = known_risk or any(
            IsCompanyRiskEffective(event, at) for event in observation.company_context.facts.risk_events
        )
    if known_risk:
        announcements: ResearchComponentStatus = "known_risk"
    elif not observation.announcements_available:
        announcements = "unknown"
    elif "risk_coverage:stale" in observation.source_errors:
        announcements = "stale"
    else:
        announcements = "known_clear"
    pledge: ResearchComponentStatus = "known_clear" if observation.pledge_ratio_pct is not None else "unknown"
    unlock: ResearchComponentStatus = "known_clear" if observation.unlock_ratio_pct is not None else "unknown"
    return ResearchComponentCoverage(financial, announcements, pledge, unlock)


def DeriveMarketRegime(
    market_breadth: float | None,
    policy: MarketRegimePolicy,
) -> str:
    if market_breadth is None or not math.isfinite(market_breadth):
        return "neutral"
    if market_breadth >= policy.risk_on_breadth_min:
        return "risk_on"
    if market_breadth <= policy.risk_off_breadth_max:
        return "risk_off"
    return "neutral"


def DeriveLongResearchFeatures(
    observation: ResearchObservation,
    inputs: LongResearchInputs,
    policy: LongResearchPolicy,
    component_weights: FeatureComponentWeightPolicy,
) -> dict[str, float | None]:
    event_features = _event_features(observation, policy)
    return {
        "value_score": _valuation_score(observation.financial, inputs.price, policy),
        "growth_score": _growth_score(observation.financial, policy),
        "quality_score": _quality_score(observation.financial, policy),
        "industry_policy_score": _industry_policy_score(
            observation,
            inputs.industry_strength,
            policy,
            component_weights.industry_policy_score,
        ),
        "risk_protection_score": _protection_score(inputs, component_weights.risk_protection_score),
        "financial_deterioration": _financial_deterioration(observation.financial, policy),
        **event_features,
    }


def DeriveLatestDisclosedReportLoss(
    observation: ResearchObservation,
    observed_at: datetime,
) -> float | None:
    """Return the latest formally disclosed report loss state at ``observed_at``."""
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("financial loss observation time must be timezone-aware")
    reports = observation.financial_history or ((observation.financial,) if observation.financial is not None else ())
    disclosed = tuple(report for report in reports if report.published_at <= observed_at)
    if not disclosed:
        return None
    latest = max(disclosed, key=lambda report: (report.report_date, report.published_at))
    parent_profit = _finite_or_none(latest.parent_net_profit)
    core_profit = _finite_or_none(latest.core_net_profit)
    if any(value is not None and value < 0.0 for value in (parent_profit, core_profit)):
        return 1.0
    if parent_profit is None or core_profit is None:
        return None
    if any(item.startswith(("financial:", "company_snapshot:")) for item in observation.source_errors):
        return None
    return 0.0


def DeriveCorporateRiskFeatures(
    facts: tuple[CorporateRiskFact, ...],
    observed_at: datetime,
    *,
    window_complete: bool,
) -> dict[str, float | None]:
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("corporate risk observation time must be timezone-aware")
    effective_categories = {
        fact.category
        for fact in facts
        if fact.announced_at <= observed_at and _corporate_risk_is_effective(fact, observed_at)
    }
    values: dict[str, float | None] = {
        category.value: float(category in effective_categories) for category in CorporateRiskCategory
    }
    values["corporate_risk_window_unavailable"] = float(not window_complete)
    return values


def _corporate_risk_is_effective(fact: CorporateRiskFact, observed_at: datetime) -> bool:
    if fact.announced_at > observed_at or (
        not fact.persistent and fact.expires_at is None and fact.announced_at < observed_at - timedelta(days=365)
    ):
        return False
    if fact.expires_at is not None and observed_at >= fact.expires_at:
        return False
    if fact.resolved_at is None:
        return True
    if fact.category is CorporateRiskCategory.MAJOR_SHAREHOLDER_REDUCTION:
        return observed_at < fact.resolved_at + timedelta(days=90)
    return observed_at < fact.resolved_at


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
    return Clamp(50.0 + policy.growth_points_per_pct * sum(known) / len(known)) if known else None


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
            Clamp(50.0 + (roe * annualizer - policy.quality_roe_neutral_pct) * policy.quality_roe_points_per_pct)
        )
    parent_profit = _finite_or_none(financial.parent_net_profit)
    core_profit = _finite_or_none(financial.core_net_profit)
    if parent_profit is not None and parent_profit > 0.0 and core_profit is not None:
        scores.append(Clamp(core_profit / parent_profit * 100.0))
    return sum(scores) / len(scores) if scores else None


def _event_features(
    observation: ResearchObservation,
    policy: LongResearchPolicy,
) -> dict[str, float | None]:
    if not observation.announcements_available:
        reduction_level = None
        negative_level = None
    else:
        context = observation.company_context
        events = context.facts.risk_events if context else ()
        at = observation.loaded_at or observation.snapshot_at
        effective_events = tuple(event for event in events if at is not None and IsCompanyRiskEffective(event, at))
        negative_level = float(min(3, max((event.risk_level for event in effective_events), default=0)))
        reduction_level = (
            3
            if any(event.risk_type is CompanyRiskType.MAJOR_SHAREHOLDER_REDUCTION for event in effective_events)
            else 0
        )
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
    weights: Mapping[str, float],
) -> float | None:
    if not observation.announcements_available:
        return None
    evidence_score = 50.0
    industry_value = _finite_or_none(industry_strength)
    if industry_value is None:
        return None
    return WeightedScore(
        {"industry_strength": industry_value, "evidence_score": evidence_score},
        weights,
    )


def _protection_score(inputs: LongResearchInputs, weights: Mapping[str, float]) -> float | None:
    volatility = _finite_or_none(inputs.low_volatility_score)
    drawdown = _finite_or_none(inputs.low_drawdown_score)
    if volatility is None or drawdown is None:
        return None
    return WeightedScore(
        {"low_volatility_score": volatility, "low_drawdown_score": drawdown},
        weights,
    )


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
    return Clamp(100.0 * (zero_score_min - value) / (zero_score_min - full_score_max))


def _finite_or_none(value: float | None) -> float | None:
    if value is None:
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _strictly_increasing_nonnegative(values: tuple[float, float, float]) -> bool:
    return 0.0 <= values[0] < values[1] < values[2]


__all__ = [
    "CorporateRiskCategory",
    "CorporateRiskFact",
    "MarketRegimePolicy",
    "FinancialReport",
    "LongResearchPolicy",
    "LongResearchInputs",
    "ResearchAnnouncement",
    "ResearchObservation",
    "DeriveCorporateRiskFeatures",
    "DeriveLatestDisclosedReportLoss",
    "DeriveMarketRegime",
    "DeriveLongResearchFeatures",
]
