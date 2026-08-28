"""Immutable research audit projected from an already-built V2 decision batch."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

from trader.application.decision_events import V2DecisionCommitted
from trader.application.scored_v2_projection import ScoredV2LocalProjection
from trader.domain.recommendation.decision_identity import ScoredDecision
from trader.domain.recommendation.scored_fusion import ScoredDecisionEntry
from trader.domain.recommendation.scored_selection import ScoredDisposition
from trader.domain.recommendation.scoring import candidate_fields

LEGACY_RESEARCH_AUDIT_SCHEMA_VERSION = "v2_committed_research_audit_v1"
RESEARCH_AUDIT_SCHEMA_VERSION = "v2_committed_research_audit_v2"
ShadowMode = Literal["control_copy", "reused_facts"]

_STRUCTURED_RISK_FIELDS = (
    "major_shareholder_reduction",
    "financial_fraud_history",
    "official_investigation_history",
    "major_illegal_history",
    "fund_occupation_history",
    "illegal_guarantee_history",
    "forced_delisting_risk",
    "unlock_risk",
    "pledge_risk",
    "financial_deterioration",
)


@dataclass(frozen=True)
class V2DecisionObservation:
    event: V2DecisionCommitted
    research_audit: V2CommittedResearchAudit | None

    def __post_init__(self) -> None:
        audit = self.research_audit
        if audit is not None and (
            audit.decision_version != self.event.decision_version or audit.decision_hash != self.event.decision_hash
        ):
            raise ValueError("research audit must match committed decision identity")


@dataclass(frozen=True)
class V2ResearchCandidateAudit:
    code: str
    board: str
    industry: str
    candidate_components: tuple[tuple[str, float], ...]
    missing_mask: tuple[str, ...]
    coverage_ratio: float
    board_reliability: float
    candidate_score: float | None
    candidate_rank: int
    production_top120: bool
    preselection_status: str
    optimistic_upper_bound: float | None = None
    upper_bound_status: Literal["not_computed"] = "not_computed"
    upper_bound_protected: bool = False

    def __post_init__(self) -> None:
        if not all((self.code, self.board, self.industry, self.preselection_status)):
            raise ValueError("research candidate identity must not be empty")
        if self.candidate_rank < 0:
            raise ValueError("research candidate rank cannot be negative")
        if not 0.0 <= self.coverage_ratio <= 1.0 or not 0.0 <= self.board_reliability <= 1.0:
            raise ValueError("research candidate ratios must be in [0, 1]")
        if self.optimistic_upper_bound is not None or self.upper_bound_status != "not_computed":
            raise ValueError("Score-R1 must not manufacture an optimistic upper bound")
        if self.upper_bound_protected:
            raise ValueError("Score-R1 cannot mark upper-bound protection")
        if self.candidate_score is not None:
            _validate_scores(self.candidate_score)
        components = _score_pairs(self.candidate_components, "candidate")
        object.__setattr__(self, "candidate_components", components)
        object.__setattr__(self, "missing_mask", tuple(sorted(set(self.missing_mask))))


@dataclass(frozen=True)
class V2ResearchDecisionCandidateAudit:
    code: str
    components: tuple[tuple[str, float | None], ...]
    component_coverage_ratio: float
    base_score: float
    local_risk_codes: tuple[str, ...]
    local_risk_penalty: float
    local_score: float
    reused_deepseek_facts: bool
    fusion_applied: bool
    deepseek_risk_codes: tuple[str, ...]
    deepseek_risk_penalty: float
    final_score: float
    action: str
    selected: bool
    rank: int
    board_rank: int
    skip_reason: str

    def __post_init__(self) -> None:
        if not all((self.code, self.action, self.skip_reason)):
            raise ValueError("research decision candidate identity must not be empty")
        if self.rank < 0 or self.board_rank < 0:
            raise ValueError("research decision candidate ranks cannot be negative")
        if self.selected != (self.rank > 0):
            raise ValueError("research selected identity must match rank")
        if self.fusion_applied and not self.reused_deepseek_facts:
            raise ValueError("research fusion requires reused DeepSeek facts")
        if not 0.0 <= self.component_coverage_ratio <= 1.0:
            raise ValueError("research component coverage must be in [0, 1]")
        _validate_scores(
            self.base_score,
            self.local_risk_penalty,
            self.local_score,
            self.deepseek_risk_penalty,
            self.final_score,
        )
        object.__setattr__(self, "components", _optional_score_pairs(self.components))
        object.__setattr__(self, "local_risk_codes", tuple(sorted(set(self.local_risk_codes))))
        object.__setattr__(self, "deepseek_risk_codes", tuple(sorted(set(self.deepseek_risk_codes))))


@dataclass(frozen=True)
class V2ResearchDecisionSetAudit:
    decision_version: str
    candidates: tuple[V2ResearchDecisionCandidateAudit, ...]

    def __post_init__(self) -> None:
        if not self.decision_version:
            raise ValueError("research decision version must not be empty")
        candidates = tuple(sorted(self.candidates, key=lambda item: item.code))
        if len({item.code for item in candidates}) != len(candidates):
            raise ValueError("research decision candidates must be unique")
        object.__setattr__(self, "candidates", candidates)


@dataclass(frozen=True)
class V2ResearchRiskFactAudit:
    risk_code: str
    source: str
    observed_at: datetime
    confidence: float
    veto: bool

    def __post_init__(self) -> None:
        if not self.risk_code or not self.source:
            raise ValueError("research risk fact identity must not be empty")
        if self.observed_at.tzinfo is None:
            raise ValueError("research risk fact observed_at must be timezone-aware")
        if not math.isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("research risk fact confidence must be in [0, 1]")


@dataclass(frozen=True)
class V2ResearchPopulationAudit:
    code: str
    board: str
    industry: str
    feature_observed_at: datetime
    quote_source_time: datetime
    quote_source: str
    data_version: str
    is_st: bool
    listing_date: date | None
    is_relisted_first_session: bool | None
    is_delisting_period_first_session: bool | None
    has_delisting_name: bool
    structured_risk_values: tuple[tuple[str, float | None], ...]
    external_risk_facts: tuple[V2ResearchRiskFactAudit, ...]
    filter_reasons: tuple[str, ...]
    disposition: str
    requested_for_refresh: bool

    def __post_init__(self) -> None:
        if not all((self.code, self.board, self.industry, self.quote_source, self.data_version)):
            raise ValueError("research population identity must not be empty")
        if self.feature_observed_at.tzinfo is None or self.quote_source_time.tzinfo is None:
            raise ValueError("research population timestamps must be timezone-aware")
        risks = tuple(self.structured_risk_values)
        if tuple(name for name, _value in risks) != _STRUCTURED_RISK_FIELDS:
            raise ValueError("research population structured risk fields are incomplete")
        if any(value is not None and not math.isfinite(value) for _name, value in risks):
            raise ValueError("research population structured risk values must be finite")
        facts = tuple(sorted(self.external_risk_facts, key=lambda fact: (fact.risk_code, fact.observed_at)))
        reasons = tuple(sorted(set(self.filter_reasons)))
        object.__setattr__(self, "structured_risk_values", risks)
        object.__setattr__(self, "external_risk_facts", facts)
        object.__setattr__(self, "filter_reasons", reasons)


@dataclass(frozen=True)
class V2CommittedResearchAudit:
    decision_version: str
    decision_hash: str
    input_version: str
    hard_filter_aggregates: tuple[tuple[str, int], ...]
    passed_candidates: tuple[V2ResearchCandidateAudit, ...]
    production_local: V2ResearchDecisionSetAudit
    research_shadow: V2ResearchDecisionSetAudit
    shadow_mode: ShadowMode
    deepseek_request_delta: int = 0
    schema_version: str = RESEARCH_AUDIT_SCHEMA_VERSION
    input_observed_at: datetime | None = None
    point_in_time_population: tuple[V2ResearchPopulationAudit, ...] = ()
    point_in_time_population_hash: str = ""
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _validate_audit_identity(self)
        aggregates = _normalized_aggregates(self.hard_filter_aggregates)
        candidates = _normalized_candidates(self.passed_candidates)
        population = _normalized_population(self.point_in_time_population)
        _validate_audit_pairing(self, candidates, population)
        payload = _audit_payload(self, aggregates, candidates, population)
        object.__setattr__(self, "hard_filter_aggregates", aggregates)
        object.__setattr__(self, "passed_candidates", candidates)
        object.__setattr__(self, "point_in_time_population", population)
        object.__setattr__(self, "content_hash", _sha256(payload))


def _validate_audit_identity(audit: V2CommittedResearchAudit) -> None:
    if not all((audit.decision_version, audit.decision_hash, audit.input_version)):
        raise ValueError("committed research audit identity must not be empty")
    if audit.schema_version not in {LEGACY_RESEARCH_AUDIT_SCHEMA_VERSION, RESEARCH_AUDIT_SCHEMA_VERSION}:
        raise ValueError("committed research audit schema is invalid")
    if audit.deepseek_request_delta != 0:
        raise ValueError("research audit cannot add DeepSeek requests")
    if audit.schema_version == LEGACY_RESEARCH_AUDIT_SCHEMA_VERSION:
        if audit.input_observed_at is not None or audit.point_in_time_population or audit.point_in_time_population_hash:
            raise ValueError("legacy research audit cannot contain v2 population evidence")
        return
    if audit.input_observed_at is None or audit.input_observed_at.tzinfo is None:
        raise ValueError("research audit input_observed_at must be timezone-aware")
    if len(audit.point_in_time_population_hash) != 64 or any(
        character not in "0123456789abcdef" for character in audit.point_in_time_population_hash
    ):
        raise ValueError("research population hash is invalid")


def _normalized_aggregates(values: tuple[tuple[str, int], ...]) -> tuple[tuple[str, int], ...]:
    aggregates = tuple(sorted(values))
    if any(not reason or count < 1 for reason, count in aggregates):
        raise ValueError("research hard-filter aggregates must be positive")
    if len({reason for reason, _count in aggregates}) != len(aggregates):
        raise ValueError("research hard-filter aggregates must be unique")
    return aggregates


def _normalized_candidates(
    values: tuple[V2ResearchCandidateAudit, ...],
) -> tuple[V2ResearchCandidateAudit, ...]:
    candidates = tuple(sorted(values, key=lambda item: item.code))
    if len({item.code for item in candidates}) != len(candidates):
        raise ValueError("research passed candidates must be unique")
    return candidates


def _normalized_population(
    values: tuple[V2ResearchPopulationAudit, ...],
) -> tuple[V2ResearchPopulationAudit, ...]:
    population = tuple(sorted(values, key=lambda item: item.code))
    if len({item.code for item in population}) != len(population):
        raise ValueError("research point-in-time population must be unique")
    return population


def _validate_audit_pairing(
    audit: V2CommittedResearchAudit,
    candidates: tuple[V2ResearchCandidateAudit, ...],
    population: tuple[V2ResearchPopulationAudit, ...],
) -> None:
    passed_codes = {item.code for item in candidates}
    decision_codes = {
        item.code
        for decision_set in (audit.production_local, audit.research_shadow)
        for item in decision_set.candidates
    }
    if not decision_codes.issubset(passed_codes):
        raise ValueError("research decisions must belong to the hard-filter passed population")
    if audit.schema_version == RESEARCH_AUDIT_SCHEMA_VERSION:
        _validate_population_evidence(audit, passed_codes, population)
    if audit.research_shadow.decision_version != audit.decision_version:
        raise ValueError("research shadow must match committed decision identity")
    if audit.shadow_mode == "control_copy" and audit.production_local != audit.research_shadow:
        raise ValueError("research control copy must equal production local")
    if audit.shadow_mode == "reused_facts" and (
        audit.production_local == audit.research_shadow
        or audit.production_local.decision_version == audit.decision_version
    ):
        raise ValueError("reused-facts research shadow requires a distinct production local")


def _validate_population_evidence(
    audit: V2CommittedResearchAudit,
    passed_codes: set[str],
    population: tuple[V2ResearchPopulationAudit, ...],
) -> None:
    population_codes = {item.code for item in population}
    if not passed_codes.issubset(population_codes):
        raise ValueError("research passed candidates must belong to the point-in-time population")
    expected_hash = point_in_time_population_hash(population)
    if audit.shadow_mode == "control_copy" and not population:
        raise ValueError("research local audit requires the complete point-in-time population")
    if audit.shadow_mode == "reused_facts" and population:
        raise ValueError("research hybrid audit must reference rather than duplicate population")
    if population and audit.point_in_time_population_hash != expected_hash:
        raise ValueError("research point-in-time population hash does not match evidence")
    cutoff = audit.input_observed_at
    if cutoff is None or any(
        item.feature_observed_at > cutoff
        or item.quote_source_time > cutoff
        or any(fact.observed_at > cutoff for fact in item.external_risk_facts)
        for item in population
    ):
        raise ValueError("research population contains evidence after input_observed_at")


def build_v2_committed_research_audit(
    projection: ScoredV2LocalProjection,
    committed: ScoredDecision,
) -> V2CommittedResearchAudit:
    if committed.strategy is not projection.local.strategy:
        raise ValueError("research audit strategy must match committed decision")
    if committed.stage == "local" and committed.version != projection.local.version:
        raise ValueError("research local audit must match the production local decision")
    if committed.stage == "hybrid" and committed.parent_version != projection.local.version:
        raise ValueError("research hybrid must reference the production local decision")
    local_entries = {entry.code: entry for entry in projection.local_epoch.entries}
    production_local = _decision_set(projection.local, local_entries, reused_facts=False)
    shadow = _decision_set(committed, local_entries, reused_facts=committed.stage == "hybrid")
    population = _population_audits(projection)
    return V2CommittedResearchAudit(
        decision_version=committed.version,
        decision_hash=committed.content_hash,
        input_version=projection.native_input.input_version,
        hard_filter_aggregates=_hard_filter_aggregates(projection),
        passed_candidates=_candidate_audits(projection),
        production_local=production_local,
        research_shadow=shadow,
        shadow_mode="reused_facts" if committed.stage == "hybrid" else "control_copy",
        input_observed_at=projection.native_input.evaluated_at,
        point_in_time_population=population if committed.stage == "local" else (),
        point_in_time_population_hash=point_in_time_population_hash(population),
    )


def try_build_v2_committed_research_audit(
    projection: ScoredV2LocalProjection,
    committed: ScoredDecision,
) -> V2CommittedResearchAudit | None:
    try:
        return build_v2_committed_research_audit(projection, committed)
    except (TypeError, ValueError):
        return None


def _hard_filter_aggregates(projection: ScoredV2LocalProjection) -> tuple[tuple[str, int], ...]:
    counts: Counter[str] = Counter()
    for evaluation in projection.selection.evaluations:
        if evaluation.disposition is ScoredDisposition.REJECT:
            counts.update(
                f"{evaluation.features.quote.board.value}:{reason.code}" for reason in evaluation.filter_reasons
            )
    return tuple(sorted(counts.items()))


def _candidate_audits(projection: ScoredV2LocalProjection) -> tuple[V2ResearchCandidateAudit, ...]:
    required = candidate_fields(projection.local.strategy)
    result: list[V2ResearchCandidateAudit] = []
    for evaluation in projection.selection.evaluations:
        if evaluation.disposition is ScoredDisposition.REJECT:
            continue
        feature = evaluation.features
        production_top120 = evaluation.candidate_rank > 0
        result.append(
            V2ResearchCandidateAudit(
                code=evaluation.code,
                board=feature.quote.board.value,
                industry=feature.quote.industry.strip() or "unknown",
                candidate_components=tuple(evaluation.candidate_components.items()),
                missing_mask=tuple(name for name in required if feature.optional_value(name) is None),
                coverage_ratio=round(1.0 - feature.missing_ratio(required), 6),
                board_reliability=round(feature.board_data_reliability, 6),
                candidate_score=evaluation.candidate_score,
                candidate_rank=evaluation.candidate_audit_rank,
                production_top120=production_top120,
                preselection_status=(
                    "selected_for_full_scoring"
                    if production_top120
                    else evaluation.selection_skip_reason
                    or evaluation.candidate_audit_pruning_reason
                    or "eligible_not_loaded"
                ),
            )
        )
    return tuple(result)


def _population_audits(projection: ScoredV2LocalProjection) -> tuple[V2ResearchPopulationAudit, ...]:
    requested_codes = set(projection.native_input.requested_codes)
    result: list[V2ResearchPopulationAudit] = []
    for evaluation in projection.selection.evaluations:
        feature = evaluation.features
        quote = feature.quote
        result.append(
            V2ResearchPopulationAudit(
                code=evaluation.code,
                board=quote.board.value,
                industry=quote.industry.strip() or "unknown",
                feature_observed_at=feature.observed_at,
                quote_source_time=quote.source_time,
                quote_source=_string_value(quote.source),
                data_version=quote.data_version,
                is_st=quote.is_st or "ST" in quote.name.upper(),
                listing_date=quote.listing_date,
                is_relisted_first_session=quote.is_relisted_first_session,
                is_delisting_period_first_session=quote.is_delisting_period_first_session,
                has_delisting_name="退" in quote.name,
                structured_risk_values=tuple((name, feature.optional_value(name)) for name in _STRUCTURED_RISK_FIELDS),
                external_risk_facts=tuple(
                    V2ResearchRiskFactAudit(
                        risk_code=fact.risk_code,
                        source=_string_value(fact.source),
                        observed_at=fact.observed_at,
                        confidence=fact.confidence,
                        veto=fact.veto,
                    )
                    for fact in feature.external_risk_facts
                ),
                filter_reasons=tuple(reason.code for reason in evaluation.filter_reasons),
                disposition=evaluation.disposition.value,
                requested_for_refresh=evaluation.code in requested_codes,
            )
        )
    population = tuple(sorted(result, key=lambda item: item.code))
    if {item.code for item in population} != {
        feature.quote.code for feature in projection.native_input.market_features
    }:
        raise ValueError("research population must equal the complete native market input")
    return population


def _decision_set(
    decision: ScoredDecision,
    local_entries: dict[str, ScoredDecisionEntry],
    *,
    reused_facts: bool,
) -> V2ResearchDecisionSetAudit:
    candidates: list[V2ResearchDecisionCandidateAudit] = []
    for item in decision.items:
        entry = local_entries[item.code]
        local_codes = tuple(fact.risk_code for fact in entry.local_risk_facts)
        deepseek_codes = tuple(code for code in item.risk_codes if code not in set(local_codes))
        components = dict(item.score_components)
        fusion_applied = reused_facts and components.get("deepseek_score") is not None
        candidates.append(
            V2ResearchDecisionCandidateAudit(
                code=item.code,
                components=tuple(item.score_components),
                component_coverage_ratio=round(entry.features.board_supported_weight, 6),
                base_score=entry.score.base_score,
                local_risk_codes=local_codes,
                local_risk_penalty=entry.score.local_risk_penalty,
                local_score=item.local_score,
                reused_deepseek_facts=fusion_applied,
                fusion_applied=fusion_applied,
                deepseek_risk_codes=deepseek_codes,
                deepseek_risk_penalty=components.get("deepseek_risk_penalty") or 0.0,
                final_score=item.final_score,
                action=item.action.value,
                selected=item.selected,
                rank=item.rank,
                board_rank=entry.board_rank,
                skip_reason=item.reason,
            )
        )
    return V2ResearchDecisionSetAudit(decision.version, tuple(candidates))


def _audit_payload(
    audit: V2CommittedResearchAudit,
    aggregates: tuple[tuple[str, int], ...],
    candidates: tuple[V2ResearchCandidateAudit, ...],
    population: tuple[V2ResearchPopulationAudit, ...],
) -> bytes:
    payload = {
        "schema_version": audit.schema_version,
        "decision_version": audit.decision_version,
        "decision_hash": audit.decision_hash,
        "input_version": audit.input_version,
        "hard_filter_aggregates": aggregates,
        "passed_candidates": [_candidate_audit_payload(candidate) for candidate in candidates],
        "production_local": _decision_set_audit_payload(audit.production_local),
        "research_shadow": _decision_set_audit_payload(audit.research_shadow),
        "shadow_mode": audit.shadow_mode,
        "deepseek_request_delta": audit.deepseek_request_delta,
    }
    if audit.schema_version == RESEARCH_AUDIT_SCHEMA_VERSION:
        payload.update(
            {
                "input_observed_at": (
                    audit.input_observed_at.isoformat() if audit.input_observed_at is not None else None
                ),
                "point_in_time_population": [_population_audit_payload(item) for item in population],
                "point_in_time_population_hash": audit.point_in_time_population_hash,
            }
        )
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _population_audit_payload(item: V2ResearchPopulationAudit) -> dict[str, object]:
    return {
        "code": item.code,
        "board": item.board,
        "industry": item.industry,
        "feature_observed_at": item.feature_observed_at.isoformat(),
        "quote_source_time": item.quote_source_time.isoformat(),
        "quote_source": item.quote_source,
        "data_version": item.data_version,
        "is_st": item.is_st,
        "listing_date": item.listing_date.isoformat() if item.listing_date is not None else None,
        "is_relisted_first_session": item.is_relisted_first_session,
        "is_delisting_period_first_session": item.is_delisting_period_first_session,
        "has_delisting_name": item.has_delisting_name,
        "structured_risk_values": item.structured_risk_values,
        "external_risk_facts": [
            {
                "risk_code": fact.risk_code,
                "source": fact.source,
                "observed_at": fact.observed_at.isoformat(),
                "confidence": fact.confidence,
                "veto": fact.veto,
            }
            for fact in item.external_risk_facts
        ],
        "filter_reasons": item.filter_reasons,
        "disposition": item.disposition,
        "requested_for_refresh": item.requested_for_refresh,
    }


def point_in_time_population_hash(population: tuple[V2ResearchPopulationAudit, ...]) -> str:
    payload = [_population_audit_payload(item) for item in population]
    return _sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())


def _string_value(value: object) -> str:
    enum_value = getattr(value, "value", value)
    result = str(enum_value).strip()
    if not result:
        raise ValueError("research population source must not be empty")
    return result


def _candidate_audit_payload(candidate: V2ResearchCandidateAudit) -> dict[str, object]:
    return {
        "code": candidate.code,
        "board": candidate.board,
        "industry": candidate.industry,
        "candidate_components": candidate.candidate_components,
        "missing_mask": candidate.missing_mask,
        "coverage_ratio": candidate.coverage_ratio,
        "board_reliability": candidate.board_reliability,
        "candidate_score": candidate.candidate_score,
        "candidate_rank": candidate.candidate_rank,
        "production_top120": candidate.production_top120,
        "preselection_status": candidate.preselection_status,
        "optimistic_upper_bound": candidate.optimistic_upper_bound,
        "upper_bound_status": candidate.upper_bound_status,
        "upper_bound_protected": candidate.upper_bound_protected,
    }


def _decision_set_audit_payload(decision_set: V2ResearchDecisionSetAudit) -> dict[str, object]:
    return {
        "decision_version": decision_set.decision_version,
        "candidates": [_decision_candidate_audit_payload(candidate) for candidate in decision_set.candidates],
    }


def _decision_candidate_audit_payload(candidate: V2ResearchDecisionCandidateAudit) -> dict[str, object]:
    return {
        "code": candidate.code,
        "components": candidate.components,
        "component_coverage_ratio": candidate.component_coverage_ratio,
        "base_score": candidate.base_score,
        "local_risk_codes": candidate.local_risk_codes,
        "local_risk_penalty": candidate.local_risk_penalty,
        "local_score": candidate.local_score,
        "reused_deepseek_facts": candidate.reused_deepseek_facts,
        "fusion_applied": candidate.fusion_applied,
        "deepseek_risk_codes": candidate.deepseek_risk_codes,
        "deepseek_risk_penalty": candidate.deepseek_risk_penalty,
        "final_score": candidate.final_score,
        "action": candidate.action,
        "selected": candidate.selected,
        "rank": candidate.rank,
        "board_rank": candidate.board_rank,
        "skip_reason": candidate.skip_reason,
    }


def _score_pairs(values: tuple[tuple[str, float], ...], label: str) -> tuple[tuple[str, float], ...]:
    normalized = tuple(sorted(values))
    if not normalized or any(
        not name or not math.isfinite(value) or not 0.0 <= value <= 100.0 for name, value in normalized
    ):
        raise ValueError(f"research {label} components are invalid")
    if len({name for name, _value in normalized}) != len(normalized):
        raise ValueError(f"research {label} components must be unique")
    return normalized


def _optional_score_pairs(
    values: tuple[tuple[str, float | None], ...],
) -> tuple[tuple[str, float | None], ...]:
    normalized = tuple(sorted(values))
    if not normalized or any(
        not name or (value is not None and (not math.isfinite(value) or not 0.0 <= value <= 100.0))
        for name, value in normalized
    ):
        raise ValueError("research local components are invalid")
    if len({name for name, _value in normalized}) != len(normalized):
        raise ValueError("research local components must be unique")
    return normalized


def _validate_scores(*values: float) -> None:
    if any(not math.isfinite(value) or not 0.0 <= value <= 100.0 for value in values):
        raise ValueError("research scores must be in [0, 100]")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "LEGACY_RESEARCH_AUDIT_SCHEMA_VERSION",
    "RESEARCH_AUDIT_SCHEMA_VERSION",
    "V2CommittedResearchAudit",
    "V2DecisionObservation",
    "V2ResearchCandidateAudit",
    "V2ResearchDecisionCandidateAudit",
    "V2ResearchDecisionSetAudit",
    "V2ResearchPopulationAudit",
    "V2ResearchRiskFactAudit",
    "build_v2_committed_research_audit",
    "point_in_time_population_hash",
    "try_build_v2_committed_research_audit",
]
