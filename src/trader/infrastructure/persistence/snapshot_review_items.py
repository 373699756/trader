"""Risk, DeepSeek review, and evidence snapshot codecs."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime

from trader.domain.models import DeepSeekReview, DimensionAssessment, Evidence, ReviewOutcome, RiskFact
from trader.infrastructure.persistence.snapshot_primitives import (
    _number,
    _optional_integer,
    _optional_number,
    _optional_text,
    _text,
)


def _risk_fact_to_dict(fact: RiskFact) -> dict[str, object]:
    return {
        "risk_fact_id": fact.risk_fact_id,
        "risk_code": fact.risk_code,
        "severity": fact.severity,
        "penalty": fact.penalty,
        "source": fact.source,
        "observed_at": fact.observed_at.isoformat(),
        "confidence": fact.confidence,
        "evidence_ids": list(fact.evidence_ids),
        "group": fact.group,
        "veto": fact.veto,
        "threshold": fact.threshold,
        "actual": fact.actual,
        "assessment": fact.assessment,
    }


def _risk_fact_from_dict(raw: Mapping[str, object]) -> RiskFact:
    evidence_ids = raw.get("evidence_ids")
    return RiskFact(
        risk_fact_id=_text(raw, "risk_fact_id"),
        risk_code=_text(raw, "risk_code"),
        severity=_text(raw, "severity"),
        penalty=_number(raw, "penalty"),
        source=_text(raw, "source"),
        observed_at=datetime.fromisoformat(_text(raw, "observed_at")),
        confidence=_number(raw, "confidence"),
        evidence_ids=tuple(str(value) for value in evidence_ids if isinstance(value, str))
        if isinstance(evidence_ids, list)
        else (),
        group=str(raw.get("group") or ""),
        veto=bool(raw.get("veto")),
        threshold=str(raw.get("threshold") or ""),
        actual=_risk_actual(raw.get("actual")),
        assessment=str(raw.get("assessment") or ""),
    )


def _risk_actual(raw: object) -> str | float | bool | None:
    if raw is None or isinstance(raw, (str, bool)):
        return raw
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and math.isfinite(float(raw)):
        return float(raw)
    raise ValueError("risk fact actual must be a finite JSON scalar")


def _review_to_dict(review: DeepSeekReview) -> dict[str, object]:
    return {
        "code": review.code,
        "outcome": review.outcome.value,
        "rating": review.rating,
        "dimensions": {
            name: {
                "name": dimension.name,
                "score": dimension.score,
                "confidence": dimension.confidence,
                "assessment": dimension.assessment,
                "flags": list(dimension.flags),
                "evidence_ids": list(dimension.evidence_ids),
                "is_unknown": dimension.is_unknown,
            }
            for name, dimension in review.dimensions.items()
        },
        "risk_facts": [_risk_fact_to_dict(fact) for fact in review.risk_facts],
        "review_stage": review.review_stage,
        "challenger_status": review.challenger_status,
        "requested_model": review.requested_model,
        "actual_model": review.actual_model,
        "thinking_mode": review.thinking_mode,
        "raw_confidence": review.raw_confidence,
        "calibrated_confidence": review.calibrated_confidence,
        "evidence_manifest_hash": review.evidence_manifest_hash,
        "calibration_version": review.calibration_version,
        "model_role": review.model_role,
        "reasoning_effort": review.reasoning_effort,
        "system_fingerprint": review.system_fingerprint,
        "prompt_cache_hit_tokens": review.prompt_cache_hit_tokens,
        "prompt_cache_miss_tokens": review.prompt_cache_miss_tokens,
        "challenger_requested_model": review.challenger_requested_model,
        "challenger_actual_model": review.challenger_actual_model,
        "challenger_thinking_mode": review.challenger_thinking_mode,
        "challenger_reasoning_effort": review.challenger_reasoning_effort,
        "challenger_system_fingerprint": review.challenger_system_fingerprint,
        "challenger_prompt_cache_hit_tokens": review.challenger_prompt_cache_hit_tokens,
        "challenger_prompt_cache_miss_tokens": review.challenger_prompt_cache_miss_tokens,
        "completed_at": review.completed_at.isoformat(),
        "error": review.error,
    }


def _review_from_dict(raw: Mapping[str, object]) -> DeepSeekReview:
    dimensions_raw = raw.get("dimensions")
    risks_raw = raw.get("risk_facts")
    dimensions: dict[str, DimensionAssessment] = {}
    if isinstance(dimensions_raw, dict):
        for name, item in dimensions_raw.items():
            if not isinstance(item, dict):
                continue
            flags = item.get("flags")
            evidence_ids = item.get("evidence_ids")
            dimensions[str(name)] = DimensionAssessment(
                name=_text(item, "name"),
                score=_number(item, "score"),
                confidence=_number(item, "confidence"),
                assessment=_text(item, "assessment"),
                flags=tuple(str(value) for value in flags if isinstance(value, str)) if isinstance(flags, list) else (),
                evidence_ids=tuple(str(value) for value in evidence_ids if isinstance(value, str))
                if isinstance(evidence_ids, list)
                else (),
                is_unknown=bool(item.get("is_unknown")),
            )
    return DeepSeekReview(
        code=_text(raw, "code"),
        outcome=ReviewOutcome(_text(raw, "outcome")),
        dimensions=dimensions,
        risk_facts=tuple(_risk_fact_from_dict(item) for item in risks_raw if isinstance(item, dict))
        if isinstance(risks_raw, list)
        else (),
        completed_at=datetime.fromisoformat(_text(raw, "completed_at")),
        error=str(raw.get("error") or ""),
        review_stage=_optional_text(raw, "review_stage") or "primary",
        challenger_status=_optional_text(raw, "challenger_status") or "not_run",
        requested_model=_optional_text(raw, "requested_model"),
        actual_model=_optional_text(raw, "actual_model"),
        thinking_mode=_optional_text(raw, "thinking_mode"),
        raw_confidence=_optional_number(raw.get("raw_confidence")),
        calibrated_confidence=_optional_number(raw.get("calibrated_confidence")),
        evidence_manifest_hash=_optional_text(raw, "evidence_manifest_hash"),
        calibration_version=_optional_text(raw, "calibration_version"),
        model_role=_optional_text(raw, "model_role"),
        reasoning_effort=_optional_text(raw, "reasoning_effort"),
        system_fingerprint=_optional_text(raw, "system_fingerprint"),
        prompt_cache_hit_tokens=_optional_integer(raw.get("prompt_cache_hit_tokens")),
        prompt_cache_miss_tokens=_optional_integer(raw.get("prompt_cache_miss_tokens")),
        challenger_requested_model=_optional_text(raw, "challenger_requested_model"),
        challenger_actual_model=_optional_text(raw, "challenger_actual_model"),
        challenger_thinking_mode=_optional_text(raw, "challenger_thinking_mode"),
        challenger_reasoning_effort=_optional_text(raw, "challenger_reasoning_effort"),
        challenger_system_fingerprint=_optional_text(raw, "challenger_system_fingerprint"),
        challenger_prompt_cache_hit_tokens=_optional_integer(raw.get("challenger_prompt_cache_hit_tokens")),
        challenger_prompt_cache_miss_tokens=_optional_integer(raw.get("challenger_prompt_cache_miss_tokens")),
        rating=_optional_text(raw, "rating") or "neutral",
    )


def _evidence_to_dict(evidence: Evidence) -> dict[str, object]:
    return {
        "evidence_id": evidence.evidence_id,
        "evidence_type": evidence.evidence_type,
        "title": evidence.title,
        "source": evidence.source,
        "published_at": evidence.published_at.isoformat(),
        "received_at": evidence.received_at.isoformat() if evidence.received_at is not None else None,
        "data_version": evidence.data_version,
    }


def _evidence_from_dict(raw: Mapping[str, object]) -> Evidence:
    received_at = raw.get("received_at")
    return Evidence(
        evidence_id=_text(raw, "evidence_id"),
        evidence_type=_text(raw, "evidence_type"),
        title=_text(raw, "title"),
        source=_text(raw, "source"),
        published_at=datetime.fromisoformat(_text(raw, "published_at")),
        received_at=datetime.fromisoformat(received_at) if isinstance(received_at, str) else None,
        data_version=str(raw.get("data_version") or ""),
    )
