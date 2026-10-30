"""Pure company adjustments and the single bounded, decimal score ledger."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import date, datetime, time
from decimal import ROUND_HALF_UP, Decimal

from trader.download.domain.company_events import (
    CompanyDataState,
    CompanyEventPolicy,
    CompanyPositiveStatus,
    CompanyRiskEvent,
    CompanyRiskType,
)
from trader.download.domain.company_facts import CompanyFacts
from trader.recommendation.domain.evidence.review import RiskFact
from trader.recommendation.domain.publication.score_ledger import ScoreAdjustmentItem, ScoreLedger


@dataclass(frozen=True, slots=True)
class CompanyScoringPolicy:
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

    def __post_init__(self) -> None:
        values = (
            self.tomorrow_cap,
            self.d25_cap,
            self.financial_max_bonus,
            self.financial_neutral,
            *self.financial_weights,
            *self.positive_points,
            *self.risk_points,
        )
        if type(self.enabled) is not bool or any(not math.isfinite(x) or x < 0 for x in values):
            raise ValueError("company_scoring_policy_invalid")
        if not 0 <= self.financial_neutral < 100 or abs(sum(self.financial_weights) - 1) > 1e-9:
            raise ValueError("company_financial_policy_invalid")
        if (
            len(self.financial_weights) != 3
            or len(self.positive_points) != 3
            or len(self.risk_points) != 3
            or min(self.tomorrow_sessions, self.d25_sessions) < 1
        ):
            raise ValueError("company_level_policy_invalid")


@dataclass(frozen=True, slots=True)
class CompanyEventWindow:
    event_id: str
    level: int
    qualified_at: datetime
    tomorrow_until: datetime | None
    d25_until: datetime | None


@dataclass(frozen=True, slots=True)
class CompanyScoringContext:
    facts: CompanyFacts
    windows: tuple[CompanyEventWindow, ...]
    coverage_current: bool


@dataclass(frozen=True, slots=True)
class CompanyScoringCalendar:
    open_dates: tuple[date, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "open_dates", tuple(sorted(set(self.open_dates))))


@dataclass(frozen=True, slots=True)
class CompanyAdjustments:
    bonuses: tuple[ScoreAdjustmentItem, ...]
    risks: tuple[RiskFact, ...]
    risk_level: int | None
    coverage_end: date | None


@dataclass(frozen=True, slots=True)
class CompanyFinancialScores:
    quality: float | None
    value: float | None
    growth: float | None


@dataclass(frozen=True, slots=True)
class ScoreLedgerInput:
    weighted_base_score: float
    adjustments: CompanyAdjustments
    risk_facts: tuple[RiskFact, ...]
    financial_contribution: float = 0.0
    growth_contribution: float = 0.0


def BuildCompanyScoringContext(
    facts: CompanyFacts,
    calendar: CompanyScoringCalendar,
    policy: CompanyScoringPolicy,
) -> CompanyScoringContext:
    days = calendar.open_dates
    windows: list[CompanyEventWindow] = []
    for event in facts.positive_events:
        for milestone in event.level_milestones:
            # A truncated calendar must not move an old event's window into the present.
            future = (
                tuple(day for day in days if day > milestone.qualified_at.date())
                if days and days[0] <= milestone.qualified_at.date()
                else ()
            )
            windows.append(
                CompanyEventWindow(
                    event.event_id,
                    milestone.level,
                    milestone.qualified_at,
                    _expiry(future, policy.tomorrow_sessions, milestone.qualified_at),
                    _expiry(future, policy.d25_sessions, milestone.qualified_at),
                )
            )
    covered = all(
        state is CompanyDataState.COMPLETE
        for state in (facts.announcement_state, facts.financial_state, facts.pledge_state, facts.unlock_state)
    )
    return CompanyScoringContext(facts, tuple(windows), covered)


def IsCompanyRiskEffective(event: CompanyRiskEvent, at: datetime) -> bool:
    if at.utcoffset() is None:
        raise ValueError("company_event_time_requires_timezone")
    if at < event.effective_at or at < event.first_disclosed_at:
        return False
    if event.expires_at is not None and at >= event.expires_at:
        return False
    if event.resolved_at is not None and at >= event.resolved_at:
        return event.risk_type is CompanyRiskType.MAJOR_SHAREHOLDER_REDUCTION and event.expires_at is not None
    return True


def AssessCompanyAdjustments(
    context: CompanyScoringContext | None,
    policy: CompanyScoringPolicy,
    strategy: str,
    observed_at: datetime,
    *,
    financial_scores: CompanyFinancialScores,
) -> CompanyAdjustments:
    if observed_at.utcoffset() is None or strategy not in {"tomorrow", "d25"}:
        raise ValueError("company_assessment_coordinates_invalid")
    if context is None:
        return CompanyAdjustments((), (), None, None)
    facts = context.facts
    effective_risks = tuple(event for event in facts.risk_events if IsCompanyRiskEffective(event, observed_at))
    level = (
        max((event.risk_level for event in effective_risks), default=0)
        if context.coverage_current or effective_risks
        else None
    )
    corporate = max(
        (event for event in effective_risks if event.risk_level < 4),
        key=lambda event: (event.risk_level, event.event_id),
        default=None,
    )
    risks = (
        ()
        if corporate is None or not policy.enabled
        else (
            RiskFact(
                f"company:{corporate.event_id}",
                "company_regulatory",
                ("low", "medium", "high")[corporate.risk_level - 1],
                policy.risk_points[corporate.risk_level - 1],
                "issuer_disclosure",
                corporate.last_disclosed_at,
                evidence_ids=corporate.evidence_ids,
                group="company_regulatory",
                actual=corporate.risk_level,
                assessment=corporate.summary,
            ),
        )
    )
    blocked = (
        "company_policy_pending"
        if not policy.enabled
        else "company_risk_active"
        if effective_risks
        else "company_coverage_incomplete"
        if not context.coverage_current
        else ""
    )
    bonuses = [
        ScoreAdjustmentItem(
            f"review:{item.scope}:{item.reason_code}:{item.evidence_id or 'coverage'}",
            "review",
            item.summary,
            0,
            0,
            0,
            item.reason_code,
            (item.evidence_id,) if item.evidence_id else (),
        )
        for item in facts.review_reasons
    ]
    if strategy == "tomorrow":
        scores = (financial_scores.quality, financial_scores.value, financial_scores.growth)
        complete = facts.financial_state is CompanyDataState.COMPLETE and all(
            value is not None and math.isfinite(value) and 0 <= value <= 100 for value in scores
        )
        financial = sum((value or 0) * weight for value, weight in zip(scores, policy.financial_weights, strict=True))
        points = (
            policy.financial_max_bonus
            * max(0.0, min(1.0, (financial - policy.financial_neutral) / (100 - policy.financial_neutral)))
            if complete
            else 0.0
        )
        reason = blocked or (
            "financial_inputs_incomplete"
            if not complete
            else "below_financial_threshold"
            if points == 0
            else "eligible"
        )
        bonuses.append(ScoreAdjustmentItem("financial", "financial_bonus", "财务奖励", 0, points, 0.0, reason))
    labels = {"share_repurchase": "实际回购", "shareholder_increase": "重要股东增持", "operating_contract": "经营合同"}
    for event in facts.positive_events:
        eligible = tuple(
            window
            for window in context.windows
            if window.event_id == event.event_id
            and window.level <= event.positive_level
            and window.qualified_at <= observed_at
            and (end := window.tomorrow_until if strategy == "tomorrow" else window.d25_until) is not None
            and observed_at <= end
        )
        chosen = max(eligible, key=lambda window: window.level, default=None)
        event_level = chosen.level if chosen else event.positive_level
        points = policy.positive_points[event_level - 1] if event_level else 0.0
        cancelled = event.cancelled_at is not None and event.cancelled_at <= observed_at
        reason = blocked or (
            "event_cancelled"
            if cancelled or event.status is CompanyPositiveStatus.CANCELLED
            else "not_implemented"
            if event.status is CompanyPositiveStatus.PLANNED
            else event.not_eligible_reason or "below_event_threshold"
            if not event.positive_level
            else "event_expired_or_calendar_missing"
            if chosen is None
            else "eligible"
        )
        until = (chosen.tomorrow_until if strategy == "tomorrow" else chosen.d25_until) if chosen else None
        bonuses.append(
            ScoreAdjustmentItem(
                event.event_id,
                "positive_bonus",
                labels[event.event_type.value],
                event_level,
                points,
                0.0,
                reason,
                event.evidence_ids,
                until,
            )
        )
    # Each positive type is one group. The label is fixed by that type, never supplier free text.
    grouped: list[ScoreAdjustmentItem] = []
    for item in sorted(bonuses, key=lambda item: (-item.rule_points, item.code)):
        if item.reason == "eligible" and any(
            previous.kind == item.kind and previous.label == item.label and previous.reason == "eligible"
            for previous in grouped
        ):
            item = replace(item, reason="same_group_lower")
        grouped.append(item)
    return CompanyAdjustments(
        tuple(sorted(grouped, key=lambda item: (item.kind != "financial_bonus", -item.rule_points, item.code))),
        risks,
        level,
        facts.announcement_coverage_end,
    )


def BuildScoreLedger(
    inputs: ScoreLedgerInput,
    *,
    strategy: str,
    policy: CompanyScoringPolicy,
    risk_cap: float,
) -> ScoreLedger:
    weighted_base_score = inputs.weighted_base_score
    adjustments = inputs.adjustments
    risk_facts = inputs.risk_facts
    if strategy not in {"tomorrow", "d25"}:
        raise ValueError("company_ledger_strategy_invalid")
    if not math.isfinite(weighted_base_score) or not 0 <= weighted_base_score <= 100:
        raise ValueError("company_base_score_invalid")
    if not math.isfinite(risk_cap) or not 0 <= risk_cap <= 100:
        raise ValueError("company_risk_cap_invalid")
    if any(not math.isfinite(fact.penalty) or fact.penalty < 0 for fact in risk_facts):
        raise ValueError("company_risk_penalty_invalid")
    base = Decimal(str(weighted_base_score))
    requested = sum(
        (Decimal(str(item.rule_points)) for item in adjustments.bonuses if item.reason == "eligible"), Decimal(0)
    )
    cap = Decimal(str(policy.tomorrow_cap if strategy == "tomorrow" else policy.d25_cap))
    capped = min(requested, cap)
    bonus = min(capped, Decimal(100) - base)
    boosted = base + bonus
    raw_risk = sum((Decimal(str(fact.penalty)) for fact in risk_facts), Decimal(0))
    risk = min(raw_risk, Decimal(str(risk_cap)))
    applied_risk = min(risk, boosted)
    final = _round(boosted - applied_risk)
    remaining = bonus
    items: list[ScoreAdjustmentItem] = []
    for item in adjustments.bonuses:
        applied = min(remaining, Decimal(str(item.rule_points))) if item.reason == "eligible" else Decimal(0)
        remaining -= applied
        items.append(
            replace(
                item,
                applied_points=float(applied),
                reason="bonus_cap"
                if item.reason == "eligible" and applied < Decimal(str(item.rule_points))
                else item.reason,
            )
        )
    items.extend(
        ScoreAdjustmentItem(
            fact.risk_code,
            "risk",
            fact.assessment or fact.risk_code,
            3 if fact.severity == "high" else 2 if fact.severity == "medium" else 1,
            fact.penalty,
            0.0,
            "risk_group_contribution",
            fact.evidence_ids,
        )
        for fact in risk_facts
    )
    rounding = final - (_round(base) + _round(bonus) - _round(applied_risk))
    company_risk = sum(fact.penalty for fact in risk_facts if fact.group == "company_regulatory")
    return ScoreLedger(
        float(base),
        float(requested),
        float(capped),
        float(bonus),
        float(boosted),
        float(raw_risk),
        float(risk),
        float(applied_risk),
        float(final),
        float(rounding),
        inputs.financial_contribution,
        inputs.growth_contribution,
        company_risk,
        adjustments.risk_level,
        adjustments.coverage_end,
        policy.enabled,
        tuple(items),
    )


def _expiry(days: tuple[date, ...], sessions: int, reference: datetime) -> datetime | None:
    return datetime.combine(days[sessions - 1], time(15), reference.tzinfo) if len(days) >= sessions else None


def _round(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
