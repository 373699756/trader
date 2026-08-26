"""Explicit serializers for unified V2 decision HTTP and SSE boundaries."""

from __future__ import annotations

from datetime import datetime

from trader.application.decision_queries import DecisionItemView, DecisionView
from trader.application.decision_stream import (
    DecisionEventPayload,
    DecisionReplacementPatch,
    OverlayEventPayload,
    ResyncEventPayload,
    UnifiedPublishedEvent,
)
from trader.domain.recommendation.decision_identity import DecisionItem, DecisionQuote


def serialize_decision_view(view: DecisionView) -> dict[str, object]:
    return {
        "schema_version": view.schema_version,
        "status": view.status,
        "strategy": view.strategy.value,
        "trade_date": view.trade_date.isoformat() if view.trade_date is not None else None,
        "view": view.view,
        "score_status": view.score_status,
        "decision_version": view.decision_version,
        "projection_version": view.projection_version,
        "content_hash": view.content_hash,
        "observed_at": _time(view.observed_at),
        "data_age_seconds": view.data_age_seconds,
        "stage": view.stage,
        "frozen": view.frozen,
        "frozen_at": _time(view.frozen_at),
        "freeze_kind": view.freeze_kind,
        "input_versions": dict(view.input_versions),
        "coverage": {
            "candidate_count": view.coverage.candidate_count,
            "evaluated_count": view.coverage.evaluated_count,
            "rejected_count": view.coverage.rejected_count,
            "selected_count": view.coverage.selected_count,
            "executable_count": view.coverage.executable_count,
            "observation_count": view.coverage.observation_count,
        },
        "filter_reason_counts": dict(view.filter_reason_counts),
        "selection_diagnostics": (
            {
                "maximum_final_score": view.selection_diagnostics.maximum_final_score,
                "executable_threshold": view.selection_diagnostics.executable_threshold,
                "observation_floor": view.selection_diagnostics.observation_floor,
                "executable_limit": view.selection_diagnostics.executable_limit,
                "observation_limit": view.selection_diagnostics.observation_limit,
                "selected_executable_count": view.selection_diagnostics.selected_executable_count,
                "selected_observation_count": view.selection_diagnostics.selected_observation_count,
                "review_candidate_count": view.selection_diagnostics.review_candidate_count,
                "empty_reason": view.selection_diagnostics.empty_reason,
            }
            if view.selection_diagnostics is not None
            else None
        ),
        "degraded_reasons": list(view.degraded_reasons),
        "items": [serialize_decision_item(item) for item in view.items],
        "draft": (
            {
                "decision_version": view.draft.decision_version,
                "content_hash": view.draft.content_hash,
                "observed_at": _time(view.draft.observed_at),
                "items": [serialize_decision_item(item) for item in view.draft.items],
            }
            if view.draft is not None
            else None
        ),
    }


def serialize_decision_item(item: DecisionItemView) -> dict[str, object]:
    return {
        "code": item.code,
        "name": item.name,
        "industry": item.industry,
        "group": item.group,
        "selected": item.selected,
        "rank": item.rank,
        "action": item.action,
        "action_reason": item.action_reason,
        "score_status": item.score_status,
        "scores": {
            "candidate": item.candidate_score,
            "local": item.local_score,
            "deepseek": item.deepseek_score,
            "deepseek_risk_penalty": item.deepseek_risk_penalty,
            "final": item.final_score,
        },
        "risk_codes": list(item.risk_codes),
        "setup": {"type": item.setup_type} if item.setup_type is not None else None,
        "downside": (
            {
                "status": item.downside_status,
                "reasons": list(item.downside_reasons),
                "atr20_pct": item.downside_atr20_pct,
                "intraday_reversal_atr": item.downside_intraday_reversal_atr,
                "historical_drawdown_pct": item.downside_historical_drawdown_pct,
            }
            if item.downside_status is not None
            else None
        ),
        "review_outcome": item.review_outcome,
        "research_coverage": (
            {
                "evidence_count": item.research_evidence_count,
                "structured_risk_fact_count": item.research_risk_fact_count,
                "review_eligible": item.review_eligible,
            }
            if item.research_evidence_count is not None
            else None
        ),
        "anchor_quote": {
            "price": item.anchor_price,
            "source": item.anchor_source,
            "source_time": _time(item.anchor_time),
        },
        "quote": {
            "price": item.price,
            "pct_change": item.pct_change,
            "amount": item.amount,
            "turnover_rate": item.turnover_rate,
            "market_cap": item.market_cap,
            "source": item.quote_source,
            "source_time": _time(item.quote_time),
            "status": item.quote_status,
        },
    }


def serialize_event(event: UnifiedPublishedEvent) -> dict[str, object]:
    payload = event.payload
    common: dict[str, object] = {"schema_version": "v2_event_v1"}
    if isinstance(payload, DecisionEventPayload):
        common.update(
            strategy=payload.strategy.value,
            trade_date=payload.trade_date,
            version=payload.version,
            content_hash=payload.content_hash,
            stage=payload.stage,
        )
        if payload.replacement is not None:
            common.update(_serialize_decision_replacement(payload, payload.replacement))
    elif isinstance(payload, OverlayEventPayload):
        common.update(
            strategy=payload.strategy.value,
            trade_date=payload.trade_date,
            version=payload.version,
            parent_version=payload.parent_version,
            content_hash=payload.content_hash,
            snapshot_id=payload.parent_version,
            projection_version=payload.projection_version,
            patch_schema_version=2,
            quotes=[_serialize_overlay_quote(quote) for quote in payload.quotes],
        )
    elif isinstance(payload, ResyncEventPayload):
        common["reason"] = payload.reason
    return common


def serialize_error(code: str, message: str) -> dict[str, object]:
    return {
        "schema_version": "v2_error_v1",
        "error": {"code": code, "message": message},
    }


def _time(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _serialize_overlay_quote(quote: DecisionQuote) -> dict[str, object]:
    return {
        "code": quote.code,
        "price": quote.price,
        "pct_change": quote.pct_change,
        "amount": quote.amount,
        "turnover_rate": quote.turnover_rate,
        "market_cap": quote.market_cap,
        "source": quote.source,
        "source_time": _time(quote.source_time),
        "quote_status": "live",
    }


def _serialize_decision_replacement(
    payload: DecisionEventPayload,
    replacement: DecisionReplacementPatch,
) -> dict[str, object]:
    diagnostics = replacement.selection_diagnostics
    return {
        "patch_schema_version": 2,
        "snapshot_id": payload.version,
        "projection_version": replacement.projection_version,
        "etag": replacement.projection_version,
        "replace": True,
        "base_projection_version": "",
        "removed_codes": [],
        "removals": [],
        "upserts": [_serialize_decision_patch_item(item) for item in replacement.items],
        "view": "live",
        "current_trade_date": payload.trade_date,
        "phase": payload.stage,
        "published_at": _time(replacement.observed_at),
        "strategy_version": replacement.strategy_version,
        "fusion_mode": replacement.fusion_mode,
        "stale": False,
        "frozen": False,
        "degraded_reasons": list(replacement.degraded_reasons),
        "filtered_count": replacement.filtered_count,
        "selection_diagnostics": (
            {
                "maximum_final_score": diagnostics.maximum_final_score,
                "executable_threshold": diagnostics.executable_threshold,
                "observation_floor": diagnostics.observation_floor,
                "executable_limit": diagnostics.executable_limit,
                "observation_limit": diagnostics.observation_limit,
                "selected_executable_count": diagnostics.selected_executable_count,
                "selected_observation_count": diagnostics.selected_observation_count,
                "review_candidate_count": diagnostics.review_candidate_count,
                "empty_reason": diagnostics.empty_reason,
            }
            if diagnostics is not None
            else {}
        ),
    }


def _serialize_decision_patch_item(item: DecisionItem) -> dict[str, object]:
    quote = item.quote
    components = dict(item.score_components)
    downside = item.downside
    coverage = item.research_coverage
    return {
        "rank": item.rank,
        "code": item.code,
        "name": item.name,
        "industry": item.industry,
        "price": quote.price if quote is not None else None,
        "pct_change": quote.pct_change if quote is not None else None,
        "turnover_rate": quote.turnover_rate if quote is not None else None,
        "amount": quote.amount if quote is not None else None,
        "market_cap": quote.market_cap if quote is not None else None,
        "source": quote.source if quote is not None else None,
        "source_time": _time(quote.source_time) if quote is not None else None,
        "quote_status": "decision_anchor" if quote is not None else "missing",
        "action": item.action.value,
        "action_reason": item.reason,
        "anchor_price": quote.price if quote is not None else None,
        "anchor_source_time": _time(quote.source_time) if quote is not None else None,
        "setup": {"type": item.setup_type} if item.setup_type is not None else None,
        "downside": (
            {
                "status": downside.status,
                "reasons": list(downside.reasons),
                "atr20_pct": downside.atr20_pct,
                "intraday_reversal_atr": downside.intraday_reversal_atr,
                "historical_drawdown_pct": downside.historical_drawdown_pct,
            }
            if downside is not None
            else None
        ),
        "review_outcome": item.review_outcome,
        "research_coverage": (
            {
                "evidence_count": coverage.evidence_count,
                "structured_risk_fact_count": coverage.structured_risk_fact_count,
                "review_eligible": coverage.review_eligible,
            }
            if coverage is not None
            else None
        ),
        "scores": {
            "candidate_score": item.candidate_score,
            "local_score": item.local_score,
            "deepseek_score": components.get("deepseek_score"),
            "deepseek_risk_penalty": components.get("deepseek_risk_penalty"),
            "final_score": item.final_score,
        },
        "risks": [{"risk_code": code} for code in item.risk_codes],
    }


__all__ = ["serialize_decision_item", "serialize_decision_view", "serialize_error", "serialize_event"]
