"""Compare company facts on a frozen recommendation input through the local evaluator."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Literal

from trader.recommendation.application.pipeline.final_selection.freeze_projection import (
    BuildScoredLocal,
    ScoredLocalProjection,
    ScoredProjectionInputs,
)
from trader.recommendation.application.pipeline.policy import RecommendationPolicy
from trader.recommendation.application.ports.scoring import ScoredNativeInput
from trader.recommendation.domain.market.models import FeatureSnapshot
from trader.recommendation.domain.market.research import CorporateRiskCategory, ResearchComponentCoverage
from trader.recommendation.domain.publication.models import RecommendationAction, ScoredDisposition, Strategy
from trader.recommendation.domain.scoring.company import CompanyScoringContext
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.recommendation.domain.selection.scored_selection import FilterCandidateInputs


CompanyReplayArmName = Literal["baseline", "risk", "financial", "combined"]

_RISK_FIELDS = frozenset((
    *(category.value for category in CorporateRiskCategory),
    "corporate_risk_window_unavailable",
    "financial_deterioration",
    "latest_disclosed_report_loss",
    "negative_announcement_level",
    "pledge_risk",
    "reduction_or_unlock",
    "shareholder_reduction_level",
    "unlock_risk",
))
_FINANCIAL_FIELDS = frozenset(("quality_score", "value_score", "growth_score"))


@dataclass(frozen=True, slots=True)
class CompanyReplayPublication:
    identity: str
    published_at: datetime

    def __post_init__(self) -> None:
        if not self.identity or self.published_at.utcoffset() is None:
            raise ValueError("company replay requires a publication identity and zoned publication time")


@dataclass(frozen=True, slots=True)
class CompanyReplayRow:
    code: str
    disposition: ScoredDisposition
    filter_reasons: tuple[str, ...]
    optional_flags: tuple[str, ...]
    selection_reason: str
    model_gate_reason: str
    local_score: float | None
    ranking_score: float | None
    rank: int | None
    action: RecommendationAction | None
    action_reason: str
    company_coverage: ResearchComponentCoverage | None


@dataclass(frozen=True, slots=True)
class CompanyReplayDecision:
    signal_identity: str
    config_identity: str
    sequence: int
    freeze_content_hash: str
    model_identity: str | None
    cost_reference: str | None
    rank_population: str | None
    rows: tuple[CompanyReplayRow, ...]

    @property
    def seats(self) -> tuple[CompanyReplayRow, ...]:
        return tuple(sorted((row for row in self.rows if row.rank is not None), key=_seat_rank))


@dataclass(frozen=True, slots=True)
class CompanyReplayArm:
    name: CompanyReplayArmName
    decision: CompanyReplayDecision

    @property
    def seats(self) -> tuple[CompanyReplayRow, ...]:
        return self.decision.seats


@dataclass(frozen=True, slots=True)
class CompanyReplayResult:
    trade_date: date
    evaluated_at: datetime
    profile: ScoringProfileId
    strategy: Strategy
    signal_identity: str
    config_identity: str
    model_identity: str
    prediction_target_bps: int
    current_company: CompanyReplayPublication
    candidate_company: CompanyReplayPublication
    arms: tuple[CompanyReplayArm, ...]


@dataclass(frozen=True, slots=True)
class CompanyReplayInputs:
    baseline: ScoredNativeInput
    candidate_company_features: ScoredNativeInput
    current_company: CompanyReplayPublication
    candidate_company: CompanyReplayPublication
    expected_baseline: CompanyReplayDecision


def ProjectCompanyReplayDecision(projection: ScoredLocalProjection) -> CompanyReplayDecision:
    """Retain the full population and fixed local decisions, without reporting review latency."""
    native = projection.native_input
    entries = {entry.code: entry for entry in projection.stages.entries}
    directed = frozenset(feature.quote.code for feature in native.candidate_features)
    rows: list[CompanyReplayRow] = []
    for item in projection.selection.evaluations:
        entry = entries.get(item.code)
        reason = item.selection_skip_reason
        if entry is None and not (reason or item.filter_reasons):
            if item.code not in directed:
                reason = "candidate_input_unavailable"
            elif item.local_score is not None:
                reason = "not_in_fixed_seats"
        rows.append(CompanyReplayRow(
            item.code,
            item.disposition,
            tuple(reason.code for reason in item.filter_reasons),
            tuple(flag.code for flag in item.optional_flags),
            reason,
            item.execution_gate_reason,
            item.local_score,
            item.ranking_score if item.ranking_score is not None else item.local_score,
            entry.rank if entry is not None else None,
            entry.action if entry is not None else None,
            entry.action_reason if entry is not None else "",
            item.features.research_components,
        ))
    return CompanyReplayDecision(
        native.input_version,
        native.config_version,
        projection.local.sequence,
        projection.local.content_hash,
        projection.score_model_version,
        projection.cost_reference_version,
        projection.rank_population_version,
        tuple(rows),
    )


def ReplayCompanyDecisions(
    inputs: CompanyReplayInputs,
    policy: RecommendationPolicy,
    *,
    runtime: ScoredProjectionInputs,
) -> CompanyReplayResult:
    """Recompute the local chain from two normalized, point-in-time company inputs."""
    baseline = inputs.baseline
    candidate = inputs.candidate_company_features
    model = runtime.model_scoring
    status = model.status() if model is not None else None
    head = status.heads.get(baseline.strategy) if status is not None else None
    if head is None or head.prediction_target is None or not head.is_enabled or not head.model_hash:
        raise ValueError("company replay requires the pinned prediction target and model head")
    if policy.company_scoring.enabled:
        raise ValueError("company replay requires the company bonus policy to be disabled")
    discovery = runtime.population_qualifications
    cutoff = min(row.observed_at for row in baseline.market_features)
    if discovery is not None:
        cutoff = min(cutoff, discovery.request.evaluated_at)
    if not inputs.current_company.published_at <= inputs.candidate_company.published_at <= cutoff:
        raise ValueError("company replay publication is unavailable at the frozen decision input")
    if (
        inputs.current_company.identity == inputs.candidate_company.identity
        and inputs.current_company.published_at != inputs.candidate_company.published_at
    ):
        raise ValueError("company replay publication time changed without a new identity")
    _validate_matched_inputs(baseline, candidate)

    sequence = inputs.expected_baseline.sequence
    original = ProjectCompanyReplayDecision(BuildScoredLocal(baseline, policy, sequence=sequence, runtime=runtime))
    if original != inputs.expected_baseline or original.model_identity is None:
        raise ValueError("company replay baseline differs from its recorded local decision")
    risk = _replay_arm(inputs, policy, runtime, use_risk=True, use_financial=False)
    financial = _replay_arm(inputs, policy, runtime, use_risk=False, use_financial=True)
    combined = _replay_arm(inputs, policy, runtime, use_risk=True, use_financial=True)
    latest_status = model.status() if model is not None else None
    latest_head = latest_status.heads.get(baseline.strategy) if latest_status is not None else None
    if (
        latest_head is None
        or any(decision.model_identity != original.model_identity for decision in (risk, financial, combined))
        or (latest_head.profile_id, latest_head.model_hash, latest_head.prediction_target)
        != (head.profile_id, head.model_hash, head.prediction_target)
    ):
        raise ValueError("company replay model changed during evaluation")
    if (
        (financial.cost_reference, financial.rank_population) != (original.cost_reference, original.rank_population)
        or (combined.cost_reference, combined.rank_population) != (risk.cost_reference, risk.rank_population)
    ):
        raise ValueError("company replay financial overlay changed the model population")
    if not (_same_qualification(original, financial) and _same_qualification(risk, combined)):
        raise ValueError("company replay financial overlay changed risk qualification")
    return CompanyReplayResult(
        baseline.trade_date,
        baseline.evaluated_at,
        head.profile_id,
        baseline.strategy,
        original.signal_identity,
        original.config_identity,
        head.model_hash,
        head.prediction_target.threshold_bps,
        inputs.current_company,
        inputs.candidate_company,
        (
            CompanyReplayArm("baseline", original),
            CompanyReplayArm("risk", risk),
            CompanyReplayArm("financial", financial),
            CompanyReplayArm("combined", combined),
        ),
    )


def _seat_rank(row: CompanyReplayRow) -> int:
    if row.rank is None:
        raise ValueError("company replay seat requires a rank")
    return row.rank


def _same_qualification(first: CompanyReplayDecision, second: CompanyReplayDecision) -> bool:
    return len(first.rows) == len(second.rows) and all(
        (
            before.code, before.disposition, before.filter_reasons, before.optional_flags,
            before.model_gate_reason, before.company_coverage,
        ) == (
            after.code, after.disposition, after.filter_reasons, after.optional_flags,
            after.model_gate_reason, after.company_coverage,
        )
        for before, after in zip(first.rows, second.rows, strict=True)
    )


def _validate_matched_inputs(baseline: ScoredNativeInput, candidate: ScoredNativeInput) -> None:
    if (
        baseline.strategy is not candidate.strategy
        or baseline.evaluated_at != candidate.evaluated_at
        or baseline.phase != candidate.phase
        or baseline.config_version != candidate.config_version
        or baseline.requested_codes != candidate.requested_codes
        or baseline.preselect_max_age_seconds != candidate.preselect_max_age_seconds
        or baseline.score_max_age_seconds != candidate.score_max_age_seconds
        or baseline.preview_context != candidate.preview_context
    ):
        raise ValueError("company replay requires the same frozen recommendation inputs")
    for current_rows, candidate_rows in (
        (baseline.market_features, candidate.market_features),
        (baseline.candidate_features, candidate.candidate_features),
    ):
        if len(current_rows) != len(candidate_rows) or any(
            original.quote != changed.quote or original.observed_at != changed.observed_at
            for original, changed in zip(current_rows, candidate_rows, strict=True)
        ):
            raise ValueError("company replay requires matching quote rows at each input cut")
        for changed in candidate_rows:
            context = changed.company_context
            if context is not None and (
                context.facts.code != changed.quote.code
                or context.facts.published_at > changed.observed_at
                or context.facts.checked_at > changed.observed_at
            ):
                raise ValueError("company replay contains future or mismatched issuer facts")


def _replay_arm(
    inputs: CompanyReplayInputs,
    policy: RecommendationPolicy,
    runtime: ScoredProjectionInputs,
    *,
    use_risk: bool,
    use_financial: bool,
) -> CompanyReplayDecision:
    baseline = inputs.baseline
    candidate = inputs.candidate_company_features
    financial = use_financial and baseline.strategy is Strategy.D25
    fields = (_RISK_FIELDS if use_risk else frozenset()) | (_FINANCIAL_FIELDS if financial else frozenset())
    if not fields:
        return inputs.expected_baseline
    market = _overlay_features(
        baseline.market_features, candidate.market_features, fields, use_risk=use_risk, use_financial=financial,
    )
    directed = _overlay_features(
        baseline.candidate_features, candidate.candidate_features, fields, use_risk=use_risk, use_financial=financial,
    )
    if (
        inputs.current_company.identity == inputs.candidate_company.identity
        and (market != baseline.market_features or directed != baseline.candidate_features)
    ):
        raise ValueError("company replay facts changed without a new publication identity")
    discovery = runtime.population_qualifications
    variant = replace(baseline, market_features=market, candidate_features=directed)
    return ProjectCompanyReplayDecision(BuildScoredLocal(
        variant,
        policy,
        sequence=inputs.expected_baseline.sequence,
        runtime=replace(
            runtime,
            quality_batch=None,
            population_qualifications=(
                FilterCandidateInputs(replace(discovery.request, features=market)) if discovery is not None else None
            ),
        ),
    ))


def _overlay_features(
    current_rows: tuple[FeatureSnapshot, ...],
    candidate_rows: tuple[FeatureSnapshot, ...],
    fields: frozenset[str],
    *,
    use_risk: bool,
    use_financial: bool,
) -> tuple[FeatureSnapshot, ...]:
    result = []
    for current, candidate in zip(current_rows, candidate_rows, strict=True):
        values = {key: value for key, value in current.values.items() if key not in fields}
        for key in fields:
            if key in candidate.values:
                value = candidate.values[key]
                if value is not None and not math.isfinite(value):
                    raise ValueError("company replay feature must be finite or missing")
                values[key] = value
        missing = (set(current.missing_fields) - fields) | (set(candidate.missing_fields) & fields)
        reasons = {key: value for key, value in current.missing_reasons.items() if key not in fields}
        reasons.update((key, value) for key, value in candidate.missing_reasons.items() if key in fields)
        result.append(replace(
            current,
            values=values,
            missing_fields=tuple(sorted(missing)),
            missing_reasons=reasons,
            research_components=candidate.research_components if use_risk else current.research_components,
            company_context=_risk_context(current, candidate) if use_risk else current.company_context,
            d25_financial_scores=candidate.d25_financial_scores if use_financial else current.d25_financial_scores,
        ))
    return tuple(result)


def _risk_context(current: FeatureSnapshot, candidate: FeatureSnapshot) -> CompanyScoringContext | None:
    context = candidate.company_context
    if context is None:
        return None
    previous = current.company_context
    facts = replace(context.facts, positive_events=previous.facts.positive_events if previous is not None else ())
    return replace(context, facts=facts, windows=previous.windows if previous is not None else ())


__all__ = [
    "CompanyReplayArm",
    "CompanyReplayArmName",
    "CompanyReplayDecision",
    "CompanyReplayInputs",
    "CompanyReplayPublication",
    "CompanyReplayResult",
    "CompanyReplayRow",
    "ProjectCompanyReplayDecision",
    "ReplayCompanyDecisions",
]
