"""Route-level serializers for Web API responses."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from trader.domain.recommendation.models import Strategy
from trader.web.schemas import (
    API_SCHEMA_VERSION,
    empty_snapshot_envelope,
    error_envelope,
    snapshot_envelope,
)


def serialize_error(
    code: str,
    message: str,
    *,
    strategy: str | None = None,
    trade_date: str | None = None,
) -> dict[str, object]:
    return error_envelope(
        code,
        message,
        strategy=strategy,
        trade_date=trade_date,
    )


def serialize_recommendation_dates(strategy: Strategy, dates: Sequence[str]) -> dict[str, object]:
    return {
        "schema_version": API_SCHEMA_VERSION,
        "status": "ready",
        "strategy": strategy.value,
        "items": list(dates),
        "error": None,
    }


def serialize_events(
    cursor: int,
    items: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    sequences: list[int] = []
    for item in items:
        sequence = item.get("sequence")
        if isinstance(sequence, int) and not isinstance(sequence, bool):
            sequences.append(sequence)
    next_cursor = max(sequences, default=cursor)
    return {
        "schema_version": API_SCHEMA_VERSION,
        "status": "ready",
        "cursor": cursor,
        "next_cursor": next_cursor,
        "items": list(items),
        "error": None,
    }


__all__ = [
    "empty_snapshot_envelope",
    "snapshot_envelope",
    "serialize_error",
    "serialize_recommendation_dates",
    "serialize_events",
]
