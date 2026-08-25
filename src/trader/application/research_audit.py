"""Immutable research audit projected from an already-built V2 decision batch."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Literal

from trader.application.decision_events import V2DecisionCommitted
from trader.application.scored_v2_projection import ScoredV2LocalProjection
from trader.domain.recommendation.decision_identity import ScoredDecision
from trader.domain.recommendation.scored_fusion import ScoredDecisionEntry
from trader.domain.recommendation.scored_selection import ScoredDisposition
from trader.domain.recommendation.scoring import candidate_fields

RESEARCH_AUDIT_SCHEMA_VERSION = "v2_committed_research_audit_v1"
ShadowMode = Literal["control_copy", "reused_facts"]


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
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _validate_audit_identity(self)
        aggregates = _normalized_aggregates(self.hard_filter_aggregates)
        candidates = _normalized_candidates(self.passed_candidates)
        _validate_audit_pairing(self, candidates)
        payload = _audit_payload(self, aggregates, candidates)
        object.__setattr__(self, "hard_filter_aggregates", aggregates)
        object.__setattr__(self, "passed_candidates", candidates)
        object.__setattr__(self, "content_hash", _sha256(payload))


def _validate_audit_identity(audit: V2CommittedResearchAudit) -> None:
    if not all((audit.decision_version, audit.decision_hash, audit.input_version)):
        raise ValueError("committed research audit identity must not be empty")
    if audit.schema_version != RESEARCH_AUDIT_SCHEMA_VERSION:
        raise ValueError("committed research audit schema is invalid")
    if audit.deepseek_request_delta != 0:
        raise ValueError("research audit cannot add DeepSeek requests")


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


def _validate_audit_pairing(
    audit: V2CommittedResearchAudit,
    candidates: tuple[V2ResearchCandidateAudit, ...],
) -> None:
    passed_codes = {item.code for item in candidates}
    decision_codes = {
        item.code
        for decision_set in (audit.production_local, audit.research_shadow)
        for item in decision_set.candidates
    }
    if not decision_codes.issubset(passed_codes):
        raise ValueError("research decisions must belong to the hard-filter passed population")
    if audit.research_shadow.decision_version != audit.decision_version:
        raise ValueError("research shadow must match committed decision identity")
    if audit.shadow_mode == "control_copy" and audit.production_local != audit.research_shadow:
        raise ValueError("research control copy must equal production local")
    if audit.shadow_mode == "reused_facts" and (
        audit.production_local == audit.research_shadow
        or audit.production_local.decision_version == audit.decision_version
    ):
        raise ValueError("reused-facts research shadow requires a distinct production local")


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
    return V2CommittedResearchAudit(
        decision_version=committed.version,
        decision_hash=committed.content_hash,
        input_version=projection.native_input.input_version,
        hard_filter_aggregates=_hard_filter_aggregates(projection),
        passed_candidates=_candidate_audits(projection),
        production_local=production_local,
        research_shadow=shadow,
        shadow_mode="reused_facts" if committed.stage == "hybrid" else "control_copy",
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
) -> bytes:
    payload = {
        "schema_version": audit.schema_version,
        "decision_version": audit.decision_version,
        "decision_hash": audit.decision_hash,
        "input_version": audit.input_version,
        "hard_filter_aggregates": aggregates,
        "passed_candidates": candidates,
        "production_local": audit.production_local,
        "research_shadow": audit.research_shadow,
        "shadow_mode": audit.shadow_mode,
        "deepseek_request_delta": audit.deepseek_request_delta,
    }
    return json.dumps(payload, default=lambda value: value.__dict__, sort_keys=True, separators=(",", ":")).encode()


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
    "RESEARCH_AUDIT_SCHEMA_VERSION",
    "V2CommittedResearchAudit",
    "V2DecisionObservation",
    "V2ResearchCandidateAudit",
    "V2ResearchDecisionCandidateAudit",
    "V2ResearchDecisionSetAudit",
    "build_v2_committed_research_audit",
    "try_build_v2_committed_research_audit",
]
