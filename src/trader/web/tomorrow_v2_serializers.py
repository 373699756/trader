"""Explicit JSON serializers for the tomorrow v2 read model."""

from __future__ import annotations

from datetime import datetime

from trader.application.tomorrow_events import (
    TomorrowDecisionEventPayload,
    TomorrowEventStreamStatus,
    TomorrowOverlayEventPayload,
    TomorrowPublishedEvent,
    TomorrowResyncEventPayload,
)
from trader.application.tomorrow_views import (
    TomorrowDecisionItemView,
    TomorrowDecisionView,
    TomorrowSourceStatusView,
    TomorrowStatusView,
)


def serialize_tomorrow_view(view: TomorrowDecisionView) -> dict[str, object]:
    return {
        "schema_version": view.schema_version,
        "status": view.status,
        "trade_date": view.trade_date,
        "projection_version": view.projection_version,
        "decision_version": view.decision_version,
        "market_epoch_version": view.market_epoch_version,
        "feature_epoch_version": view.feature_epoch_version,
        "research_epoch_version": view.research_epoch_version,
        "quote_version": view.quote_version,
        "config_version": view.config_version,
        "strategy_version": view.strategy_version,
        "fusion_version": view.fusion_version,
        "projection_stage": view.projection_stage,
        "published_at": _time(view.published_at),
        "frozen": view.frozen,
        "frozen_at": _time(view.frozen_at),
        "freeze_kind": view.freeze_kind,
        "freeze_version": view.freeze_version,
        "data_age_seconds": view.data_age_seconds,
        "coverage": {
            "evaluated_count": view.evaluated_count,
            "rejected_count": view.rejected_count,
            "unscored_count": view.unscored_count,
            "selected_count": view.selected_count,
        },
        "filter_reason_counts": dict(view.filter_reason_counts),
        "degraded_reasons": list(view.degraded_reasons),
        "items": [serialize_tomorrow_item(item) for item in view.items],
    }


def serialize_tomorrow_item(item: TomorrowDecisionItemView) -> dict[str, object]:
    return {
        "code": item.code,
        "name": item.name,
        "industry": item.industry,
        "board": item.board,
        "rank": item.rank,
        "action": item.action,
        "action_reason": item.action_reason,
        "disposition": item.disposition,
        "current_price": item.current_price,
        "current_pct_change": item.current_pct_change,
        "quote_source": item.quote_source,
        "quote_source_time": item.quote_source_time.isoformat(),
        "quote_version": item.quote_version,
        "quote_age_seconds": item.quote_age_seconds,
        "anchor_price": item.anchor_price,
        "anchor_pct_change": item.anchor_pct_change,
        "anchor_source": item.anchor_source,
        "anchor_source_time": _time(item.anchor_source_time),
        "anchor_to_now_pct": item.anchor_to_now_pct,
        "local_score": item.local_score,
        "deepseek_score": item.deepseek_score,
        "deepseek_risk_penalty": item.deepseek_risk_penalty,
        "final_score": item.final_score,
        "fusion_mode": item.fusion_mode,
        "review_outcome": item.review_outcome,
        "local_risk_codes": list(item.local_risk_codes),
        "deepseek_risk_codes": list(item.deepseek_risk_codes),
    }


def serialize_tomorrow_status(
    status: TomorrowStatusView,
    events: TomorrowEventStreamStatus,
) -> dict[str, object]:
    return {
        "schema_version": status.schema_version,
        "status": status.status,
        "observed_at": status.observed_at.isoformat(),
        "current": {
            "decision_version": status.decision_version,
            "trade_date": status.decision_trade_date,
            "projection_stage": status.projection_stage,
            "quote_version": status.quote_version,
            "decision_age_seconds": status.decision_age_seconds,
        },
        "sources": [_source_status(item) for item in status.sources],
        "latency": {
            "pipeline_ms": status.pipeline_latency_ms,
            "publish_ms": status.publish_latency_ms,
        },
        "deepseek_budget": {
            "limit": status.deepseek_limit,
            "used": status.deepseek_used,
            "reserved": status.deepseek_reserved,
            "remaining": status.deepseek_remaining,
        },
        "events": {
            "sequence": events.sequence,
            "history_size": events.history_size,
            "subscriber_count": events.subscriber_count,
            "slow_subscriber_drops": events.slow_subscriber_drops,
        },
        "recent_failures": list(status.recent_failures),
    }


def serialize_tomorrow_event(event: TomorrowPublishedEvent) -> dict[str, object]:
    common: dict[str, object] = {
        "schema_version": 2,
        "patch_schema_version": 2,
    }
    payload = event.payload
    if isinstance(payload, TomorrowDecisionEventPayload):
        common.update(
            {
                "trade_date": payload.trade_date,
                "projection_version": payload.projection_version,
                "decision_version": payload.decision_version,
                "market_epoch_version": payload.market_epoch_version,
                "feature_epoch_version": payload.feature_epoch_version,
                "research_epoch_version": payload.research_epoch_version,
                "quote_version": payload.quote_version,
                "config_version": payload.config_version,
                "strategy_version": payload.strategy_version,
                "fusion_version": payload.fusion_version,
                "projection_stage": payload.projection_stage,
                "frozen": payload.frozen,
                "freeze_version": payload.freeze_version,
                "etag": payload.etag,
            }
        )
    elif isinstance(payload, TomorrowOverlayEventPayload):
        common.update(
            {
                "decision_version": payload.decision_version,
                "projection_version": payload.projection_version,
                "quote_version": payload.quote_version,
                "observed_at": payload.overlay.observed_at.isoformat(),
                "quotes": [
                    {
                        "code": quote.code,
                        "price": quote.price,
                        "pct_change": quote.pct_change,
                        "source": quote.source,
                        "source_time": quote.source_time.isoformat(),
                        "quote_version": quote.data_version,
                        "data_age_seconds": round(
                            max(
                                0.0,
                                (payload.overlay.observed_at - quote.source_time).total_seconds(),
                            ),
                            3,
                        ),
                    }
                    for quote in payload.overlay.quotes
                ],
            }
        )
    elif isinstance(payload, TomorrowResyncEventPayload):
        common.update(
            {
                "reason": payload.reason,
                "projection_version": payload.projection_version,
            }
        )
    return common


def serialize_tomorrow_error(code: str, message: str) -> dict[str, object]:
    return {
        "schema_version": "tomorrow_error_v2",
        "error": {"code": code, "message": message},
    }


def _source_status(item: TomorrowSourceStatusView) -> dict[str, object]:
    return {
        "name": item.name,
        "status": item.status,
        "source_time": _time(item.source_time),
        "received_at": _time(item.received_at),
        "source_age_seconds": item.source_age_seconds,
        "receive_age_seconds": item.receive_age_seconds,
    }


def _time(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


__all__ = [
    "serialize_tomorrow_error",
    "serialize_tomorrow_event",
    "serialize_tomorrow_item",
    "serialize_tomorrow_status",
    "serialize_tomorrow_view",
]
