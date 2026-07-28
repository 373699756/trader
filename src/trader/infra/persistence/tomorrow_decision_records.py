"""Canonical JSON codec for tomorrow v2 decision checkpoints and freezes."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import date, datetime
from typing import Literal, cast
from zoneinfo import ZoneInfo

from trader.domain.recommendation.models import RecommendationAction
from trader.domain.recommendation.tomorrow_freeze import (
    DecisionAnchor,
    FreezeKind,
    TomorrowDecisionFreeze,
    TomorrowFreezeCheckpoint,
)
from trader.domain.recommendation.tomorrow_fusion import (
    DecisionEpoch,
    TomorrowDecisionEntry,
)
from trader.domain.recommendation.tomorrow_selection import TomorrowDisposition
from trader.domain.review.models import ReviewOutcome, RiskFact
from trader.infra.persistence.snapshot_items import (
    _features_from_dict,
    _features_to_dict,
    _score_from_dict,
    _score_to_dict,
)
from trader.infra.persistence.snapshot_primitives import (
    _integer,
    _number,
    _object,
    _optional_number,
    _optional_text,
    _text,
)
from trader.infra.persistence.snapshot_review_items import (
    _review_from_dict,
    _review_to_dict,
    _risk_fact_from_dict,
    _risk_fact_to_dict,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")


def checkpoint_bytes(checkpoint: TomorrowFreezeCheckpoint) -> bytes:
    return _canonical_bytes(
        {
            "record_type": "tomorrow_checkpoint",
            "schema_version": checkpoint.schema_version,
            "version": checkpoint.version,
            "content_hash": checkpoint.content_hash,
            "boundary_at": checkpoint.boundary_at.isoformat(),
            "decision": _decision_to_dict(checkpoint.decision),
        }
    )


def checkpoint_from_bytes(payload: bytes) -> TomorrowFreezeCheckpoint:
    raw = _root(payload)
    if _text(raw, "record_type") != "tomorrow_checkpoint":
        raise ValueError("unexpected tomorrow checkpoint record type")
    checkpoint = TomorrowFreezeCheckpoint(
        decision=_decision_from_dict(_object(raw, "decision")),
        boundary_at=_shanghai_datetime(_text(raw, "boundary_at")),
        schema_version=_text(raw, "schema_version"),
    )
    _verify_identity(raw, checkpoint.version, checkpoint.content_hash)
    return checkpoint


def freeze_bytes(frozen: TomorrowDecisionFreeze) -> bytes:
    return _canonical_bytes(
        {
            "record_type": "tomorrow_freeze",
            "schema_version": frozen.schema_version,
            "version": frozen.version,
            "content_hash": frozen.content_hash,
            "frozen_at": frozen.frozen_at.isoformat(),
            "freeze_kind": frozen.freeze_kind,
            "checkpoint_version": frozen.checkpoint_version,
            "degraded_reasons": list(frozen.degraded_reasons),
            "anchors": [_anchor_to_dict(anchor) for anchor in frozen.anchors],
            "decision": _decision_to_dict(frozen.decision),
        }
    )


def freeze_from_bytes(payload: bytes) -> TomorrowDecisionFreeze:
    raw = _root(payload)
    if _text(raw, "record_type") != "tomorrow_freeze":
        raise ValueError("unexpected tomorrow freeze record type")
    anchors_raw = raw.get("anchors")
    reasons_raw = raw.get("degraded_reasons")
    freeze_kind_raw = _text(raw, "freeze_kind")
    if freeze_kind_raw not in {"scheduled", "checkpoint_recovery", "close_fallback"}:
        raise ValueError("unsupported tomorrow freeze kind")
    freeze_kind = cast(FreezeKind, freeze_kind_raw)
    frozen = TomorrowDecisionFreeze(
        decision=_decision_from_dict(_object(raw, "decision")),
        frozen_at=_shanghai_datetime(_text(raw, "frozen_at")),
        freeze_kind=freeze_kind,
        anchors=tuple(_anchor_from_dict(item) for item in anchors_raw if isinstance(item, dict))
        if isinstance(anchors_raw, list)
        else (),
        checkpoint_version=_optional_text(raw, "checkpoint_version"),
        degraded_reasons=tuple(str(item) for item in reasons_raw if isinstance(item, str))
        if isinstance(reasons_raw, list)
        else (),
        schema_version=_text(raw, "schema_version"),
    )
    _verify_identity(raw, frozen.version, frozen.content_hash)
    return frozen


def _decision_to_dict(decision: DecisionEpoch) -> dict[str, object]:
    return {
        "schema_version": decision.schema_version,
        "version": decision.version,
        "content_hash": decision.content_hash,
        "trade_date": decision.trade_date.isoformat(),
        "sequence": decision.sequence,
        "observed_at": decision.observed_at.isoformat(),
        "config_version": decision.config_version,
        "strategy_version": decision.strategy_version,
        "fusion_version": decision.fusion_version,
        "market_epoch_version": decision.market_epoch_version,
        "candidate_epoch_version": decision.candidate_epoch_version,
        "research_epoch_version": decision.research_epoch_version,
        "projection_stage": decision.projection_stage,
        "parent_decision_version": decision.parent_decision_version,
        "entries": [_entry_to_dict(item) for item in decision.entries],
        "review_candidate_codes": list(decision.review_candidate_codes),
        "evaluated_count": decision.evaluated_count,
        "rejected_count": decision.rejected_count,
        "unscored_count": decision.unscored_count,
        "filter_reason_counts": dict(decision.filter_reason_counts),
        "population_versions": dict(decision.population_versions),
        "degraded_reasons": list(decision.degraded_reasons),
    }


def _decision_from_dict(raw: Mapping[str, object]) -> DecisionEpoch:
    entries_raw = raw.get("entries")
    review_codes = raw.get("review_candidate_codes")
    reason_counts = raw.get("filter_reason_counts")
    populations = raw.get("population_versions")
    reasons = raw.get("degraded_reasons")
    projection_stage_raw = _text(raw, "projection_stage")
    if projection_stage_raw not in {"local", "hybrid"}:
        raise ValueError("unsupported decision projection stage")
    projection_stage = cast(Literal["local", "hybrid"], projection_stage_raw)
    decision = DecisionEpoch(
        trade_date=date.fromisoformat(_text(raw, "trade_date")),
        sequence=_integer(raw, "sequence"),
        observed_at=_shanghai_datetime(_text(raw, "observed_at")),
        config_version=_text(raw, "config_version"),
        strategy_version=_text(raw, "strategy_version"),
        fusion_version=_text(raw, "fusion_version"),
        market_epoch_version=_text(raw, "market_epoch_version"),
        candidate_epoch_version=_optional_text(raw, "candidate_epoch_version"),
        research_epoch_version=_optional_text(raw, "research_epoch_version"),
        projection_stage=projection_stage,
        parent_decision_version=_optional_text(raw, "parent_decision_version"),
        entries=tuple(_entry_from_dict(item) for item in entries_raw if isinstance(item, dict))
        if isinstance(entries_raw, list)
        else (),
        review_candidate_codes=tuple(str(item) for item in review_codes if isinstance(item, str))
        if isinstance(review_codes, list)
        else (),
        evaluated_count=_integer(raw, "evaluated_count"),
        rejected_count=_integer(raw, "rejected_count"),
        unscored_count=_integer(raw, "unscored_count"),
        filter_reason_counts={
            str(key): int(value)
            for key, value in reason_counts.items()
            if isinstance(value, int) and not isinstance(value, bool)
        }
        if isinstance(reason_counts, dict)
        else {},
        population_versions={str(key): str(value) for key, value in populations.items() if isinstance(value, str)}
        if isinstance(populations, dict)
        else {},
        degraded_reasons=tuple(str(item) for item in reasons if isinstance(item, str))
        if isinstance(reasons, list)
        else (),
        schema_version=_text(raw, "schema_version"),
    )
    _verify_identity(raw, decision.version, decision.content_hash)
    return decision


def _entry_to_dict(entry: TomorrowDecisionEntry) -> dict[str, object]:
    return {
        "features": _features_to_dict(entry.features),
        "disposition": entry.disposition.value,
        "score": _score_to_dict(entry.score),
        "action": entry.action.value,
        "action_reason": entry.action_reason,
        "selected": entry.selected,
        "rank": entry.rank,
        "candidate_score": entry.candidate_score,
        "candidate_rank": entry.candidate_rank,
        "board_rank": entry.board_rank,
        "local_risk_facts": [_risk_fact_to_dict(fact) for fact in entry.local_risk_facts],
        "deepseek_risk_facts": [_risk_fact_to_dict(fact) for fact in entry.deepseek_risk_facts],
        "review": _review_to_dict(entry.review) if entry.review is not None else None,
        "review_outcome": (entry.review_outcome.value if entry.review_outcome is not None else None),
        "veto": entry.veto,
        "local_selection_skip_reason": entry.local_selection_skip_reason,
        "decision_skip_reason": entry.decision_skip_reason,
    }


def _entry_from_dict(raw: Mapping[str, object]) -> TomorrowDecisionEntry:
    local_risks = raw.get("local_risk_facts")
    deepseek_risks = raw.get("deepseek_risk_facts")
    review_raw = raw.get("review")
    review_outcome = _optional_text(raw, "review_outcome")
    features = _features_from_dict(_object(raw, "features"))
    features = replace(
        features,
        observed_at=features.observed_at.astimezone(SHANGHAI),
        quote=replace(
            features.quote,
            source_time=features.quote.source_time.astimezone(SHANGHAI),
            received_time=features.quote.received_time.astimezone(SHANGHAI),
        ),
        external_risk_facts=tuple(
            replace(fact, observed_at=fact.observed_at.astimezone(SHANGHAI)) for fact in features.external_risk_facts
        ),
    )
    review = _review_from_dict(review_raw) if isinstance(review_raw, dict) else None
    if review is not None:
        review = replace(
            review,
            completed_at=review.completed_at.astimezone(SHANGHAI),
            risk_facts=tuple(
                replace(fact, observed_at=fact.observed_at.astimezone(SHANGHAI)) for fact in review.risk_facts
            ),
        )
    return TomorrowDecisionEntry(
        features=features,
        disposition=TomorrowDisposition(_text(raw, "disposition")),
        score=_score_from_dict(_object(raw, "score")),
        action=RecommendationAction(_text(raw, "action")),
        action_reason=_text(raw, "action_reason"),
        selected=bool(raw.get("selected")),
        rank=_integer(raw, "rank"),
        candidate_score=_optional_number(raw.get("candidate_score")),
        candidate_rank=_integer(raw, "candidate_rank"),
        board_rank=_integer(raw, "board_rank"),
        local_risk_facts=tuple(_risk_fact_from_record(item) for item in local_risks if isinstance(item, dict))
        if isinstance(local_risks, list)
        else (),
        deepseek_risk_facts=tuple(_risk_fact_from_record(item) for item in deepseek_risks if isinstance(item, dict))
        if isinstance(deepseek_risks, list)
        else (),
        review=review,
        review_outcome=ReviewOutcome(review_outcome) if review_outcome else None,
        veto=bool(raw.get("veto")),
        local_selection_skip_reason=str(raw.get("local_selection_skip_reason") or ""),
        decision_skip_reason=str(raw.get("decision_skip_reason") or ""),
    )


def _anchor_to_dict(anchor: DecisionAnchor) -> dict[str, object]:
    return {
        "code": anchor.code,
        "price": anchor.price,
        "pct_change": anchor.pct_change,
        "source": anchor.source,
        "source_time": anchor.source_time.isoformat(),
        "data_version": anchor.data_version,
    }


def _risk_fact_from_record(raw: Mapping[str, object]) -> RiskFact:
    fact = _risk_fact_from_dict(raw)
    return replace(fact, observed_at=fact.observed_at.astimezone(SHANGHAI))


def _anchor_from_dict(raw: Mapping[str, object]) -> DecisionAnchor:
    return DecisionAnchor(
        code=_text(raw, "code"),
        price=_number(raw, "price"),
        pct_change=_optional_number(raw.get("pct_change")),
        source=_text(raw, "source"),
        source_time=_shanghai_datetime(_text(raw, "source_time")),
        data_version=_text(raw, "data_version"),
    )


def _root(payload: bytes) -> Mapping[str, object]:
    raw = json.loads(payload)
    if not isinstance(raw, dict):
        raise ValueError("tomorrow decision record must be a JSON object")
    return raw


def _verify_identity(
    raw: Mapping[str, object],
    expected_version: str,
    expected_hash: str,
) -> None:
    if _text(raw, "version") != expected_version or _text(raw, "content_hash") != expected_hash:
        raise ValueError("tomorrow decision record identity verification failed")


def _canonical_bytes(raw: Mapping[str, object]) -> bytes:
    return json.dumps(
        raw,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _shanghai_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("stored tomorrow datetime must be timezone-aware")
    return parsed.astimezone(SHANGHAI)


__all__ = [
    "checkpoint_bytes",
    "checkpoint_from_bytes",
    "freeze_bytes",
    "freeze_from_bytes",
]
