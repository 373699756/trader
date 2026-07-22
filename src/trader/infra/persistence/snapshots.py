"""Canonical JSON serialization for published and frozen recommendation snapshots."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime

from trader.domain.models import FusionMode, RecommendationSnapshot, Strategy
from trader.infra.persistence.snapshot_items import (
    _filter_audit_from_dict,
    _filter_audit_to_dict,
    _recommendation_from_dict,
    _recommendation_to_dict,
)
from trader.infra.persistence.snapshot_primitives import _integer, _text
from trader.infra.persistence.snapshot_replay import _replay_input_from_dict, _replay_input_to_dict

SNAPSHOT_SCHEMA_VERSION = "recommendation_snapshot_v2"


def snapshot_to_dict(snapshot: RecommendationSnapshot) -> dict[str, object]:
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "snapshot_id": snapshot.snapshot_id,
        "strategy": snapshot.strategy.value,
        "trade_date": snapshot.trade_date,
        "phase": snapshot.phase,
        "data_version": snapshot.data_version,
        "strategy_version": snapshot.strategy_version,
        "config_version": snapshot.config_version,
        "fusion_version": snapshot.fusion_version,
        "fusion_mode": snapshot.fusion_mode.value,
        "published_at": snapshot.published_at.isoformat(),
        "filtered_count": snapshot.filtered_count,
        "filter_reasons": dict(snapshot.filter_reasons),
        "filter_details": [_filter_audit_to_dict(item) for item in snapshot.filter_details],
        "stale": snapshot.stale,
        "frozen": snapshot.frozen,
        "degraded_reasons": list(snapshot.degraded_reasons),
        "metadata": dict(snapshot.metadata),
        "replay_input": _replay_input_to_dict(snapshot.replay_input) if snapshot.replay_input is not None else None,
        "recommendations": [_recommendation_to_dict(item) for item in snapshot.recommendations],
    }


def snapshot_bytes(snapshot: RecommendationSnapshot) -> bytes:
    return json.dumps(
        snapshot_to_dict(snapshot),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def snapshot_sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def snapshot_from_dict(raw: Mapping[str, object]) -> RecommendationSnapshot:
    if raw.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise ValueError("unsupported recommendation snapshot schema")
    recommendations_raw = raw.get("recommendations")
    if not isinstance(recommendations_raw, list):
        raise ValueError("recommendations must be a list")
    recommendations = tuple(_recommendation_from_dict(item) for item in recommendations_raw if isinstance(item, dict))
    filter_reasons = raw.get("filter_reasons")
    filter_details = raw.get("filter_details")
    metadata = raw.get("metadata")
    replay_raw = raw.get("replay_input")
    degraded_raw = raw.get("degraded_reasons")
    return RecommendationSnapshot(
        snapshot_id=_text(raw, "snapshot_id"),
        strategy=Strategy(_text(raw, "strategy")),
        trade_date=_text(raw, "trade_date"),
        phase=_text(raw, "phase"),
        data_version=_text(raw, "data_version"),
        strategy_version=_text(raw, "strategy_version"),
        config_version=str(raw.get("config_version") or "legacy-unrecorded"),
        fusion_version=_text(raw, "fusion_version"),
        fusion_mode=FusionMode(_text(raw, "fusion_mode")),
        published_at=datetime.fromisoformat(_text(raw, "published_at")),
        recommendations=recommendations,
        filtered_count=_integer(raw, "filtered_count"),
        filter_reasons={str(key): int(value) for key, value in filter_reasons.items()}
        if isinstance(filter_reasons, dict)
        else {},
        filter_details=tuple(_filter_audit_from_dict(item) for item in filter_details if isinstance(item, dict))
        if isinstance(filter_details, list)
        else (),
        stale=bool(raw.get("stale")),
        frozen=bool(raw.get("frozen")),
        degraded_reasons=tuple(str(value) for value in degraded_raw if isinstance(value, str))
        if isinstance(degraded_raw, list)
        else (),
        metadata=dict(metadata) if isinstance(metadata, dict) else {},
        replay_input=_replay_input_from_dict(replay_raw) if isinstance(replay_raw, dict) else None,
    )


__all__ = [
    "SNAPSHOT_SCHEMA_VERSION",
    "snapshot_bytes",
    "snapshot_from_dict",
    "snapshot_sha256",
    "snapshot_to_dict",
]
