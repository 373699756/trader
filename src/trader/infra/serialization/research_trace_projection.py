"""JSON projection for committed research trace events."""

from __future__ import annotations

import json
from typing import cast

from trader.recommendation.application.pipeline.freeze_publish.decision_events import (
    CommittedDecisionItem,
    DecisionObservation,
)
from trader.training.application.research_audit import (
    LEGACY_RESEARCH_AUDIT_SCHEMA_VERSION,
    RESEARCH_AUDIT_SCHEMA_VERSION,
    CommittedResearchAudit,
    ResearchCandidateAudit,
    ResearchDecisionCandidateAudit,
    ResearchDecisionSetAudit,
    ResearchPopulationAudit,
    ResearchRiskFactAudit,
)


def observation_bytes(
    observation: DecisionObservation,
    *,
    schema_version: str,
    legacy_schema_version: str,
    current_schema_version: str,
) -> bytes:
    if schema_version not in {legacy_schema_version, current_schema_version}:
        raise ValueError("research event schema is invalid")
    audit = observation.research_audit
    expected_audit_schema = (
        LEGACY_RESEARCH_AUDIT_SCHEMA_VERSION
        if schema_version == legacy_schema_version
        else RESEARCH_AUDIT_SCHEMA_VERSION
    )
    if audit is not None and audit.schema_version != expected_audit_schema:
        raise ValueError("research event and audit schemas must advance together")
    event = observation.event
    payload = {
        "schema_version": schema_version,
        "event_id": event.event_id,
        "strategy": event.strategy.value,
        "trade_date": event.trade_date.isoformat(),
        "observed_at": event.observed_at.isoformat(),
        "decision_version": event.decision_version,
        "decision_hash": event.decision_hash,
        "parent_version": event.parent_version,
        "stage": event.stage,
        "input_versions": event.input_versions,
        "config_version": event.config_version,
        "strategy_version": event.strategy_version,
        "fusion_version": event.fusion_version,
        "decision_schema_version": event.schema_version,
        "filter_aggregates": event.filter_aggregates,
        "degraded_reasons": event.degraded_reasons,
        "items": tuple(_item_dict(item) for item in event.items),
        "research_audit": _audit_dict(cast(CommittedResearchAudit | None, audit)),
    }
    return json.dumps(payload, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()


def _item_dict(item: CommittedDecisionItem) -> dict[str, object]:
    return {
        "code": item.code, "action": item.action.value, "selected": item.selected, "rank": item.rank,
        "candidate_score": item.candidate_score, "local_score": item.local_score, "final_score": item.final_score,
        "score_components": item.score_components, "risk_codes": item.risk_codes, "reason": item.reason,
    }


def _audit_dict(audit: CommittedResearchAudit | None) -> dict[str, object] | None:
    if audit is None:
        return None
    payload: dict[str, object] = {
        "schema_version": audit.schema_version, "decision_version": audit.decision_version,
        "decision_hash": audit.decision_hash, "input_version": audit.input_version,
        "hard_filter_aggregates": audit.hard_filter_aggregates,
        "passed_candidates": tuple(_candidate_audit_dict(item) for item in audit.passed_candidates),
        "production_local": _decision_set_audit_dict(audit.production_local),
        "research_shadow": _decision_set_audit_dict(audit.research_shadow),
        "shadow_mode": audit.shadow_mode, "deepseek_request_delta": audit.deepseek_request_delta,
        "content_hash": audit.content_hash,
    }
    if audit.schema_version == RESEARCH_AUDIT_SCHEMA_VERSION:
        payload.update({
            "input_observed_at": audit.input_observed_at.isoformat() if audit.input_observed_at is not None else None,
            "point_in_time_population": tuple(_population_audit_dict(item) for item in audit.point_in_time_population),
            "point_in_time_population_hash": audit.point_in_time_population_hash,
        })
    return payload


def _population_audit_dict(item: ResearchPopulationAudit) -> dict[str, object]:
    return {
        "code": item.code, "board": item.board, "industry": item.industry,
        "feature_observed_at": item.feature_observed_at.isoformat(), "quote_source_time": item.quote_source_time.isoformat(),
        "quote_source": item.quote_source, "data_version": item.data_version, "is_st": item.is_st,
        "listing_date": item.listing_date.isoformat() if item.listing_date is not None else None,
        "is_relisted_first_session": item.is_relisted_first_session,
        "is_delisting_period_first_session": item.is_delisting_period_first_session,
        "has_delisting_name": item.has_delisting_name, "structured_risk_values": item.structured_risk_values,
        "external_risk_facts": tuple(_risk_fact_audit_dict(fact) for fact in item.external_risk_facts),
        "filter_reasons": item.filter_reasons, "disposition": item.disposition,
        "requested_for_refresh": item.requested_for_refresh,
    }


def _risk_fact_audit_dict(item: ResearchRiskFactAudit) -> dict[str, object]:
    return {"risk_code": item.risk_code, "source": item.source, "observed_at": item.observed_at.isoformat(), "confidence": item.confidence, "veto": item.veto}


def _candidate_audit_dict(item: ResearchCandidateAudit) -> dict[str, object]:
    return {"code": item.code, "board": item.board, "industry": item.industry, "candidate_components": item.candidate_components, "missing_mask": item.missing_mask, "coverage_ratio": item.coverage_ratio, "board_reliability": item.board_reliability, "candidate_score": item.candidate_score, "candidate_rank": item.candidate_rank, "production_top120": item.production_top120, "preselection_status": item.preselection_status, "optimistic_upper_bound": item.optimistic_upper_bound, "upper_bound_status": item.upper_bound_status, "upper_bound_protected": item.upper_bound_protected}


def _decision_set_audit_dict(item: ResearchDecisionSetAudit) -> dict[str, object]:
    return {"decision_version": item.decision_version, "candidates": tuple(_decision_candidate_audit_dict(candidate) for candidate in item.candidates)}


def _decision_candidate_audit_dict(item: ResearchDecisionCandidateAudit) -> dict[str, object]:
    return {"code": item.code, "components": item.components, "component_coverage_ratio": item.component_coverage_ratio, "base_score": item.base_score, "local_risk_codes": item.local_risk_codes, "local_risk_penalty": item.local_risk_penalty, "local_score": item.local_score, "reused_deepseek_facts": item.reused_deepseek_facts, "fusion_applied": item.fusion_applied, "deepseek_risk_codes": item.deepseek_risk_codes, "deepseek_risk_penalty": item.deepseek_risk_penalty, "final_score": item.final_score, "action": item.action, "selected": item.selected, "rank": item.rank, "board_rank": item.board_rank, "skip_reason": item.skip_reason}
