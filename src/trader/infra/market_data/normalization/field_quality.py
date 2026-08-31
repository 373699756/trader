"""Pure field-level candidate selection helpers for deterministic market merge.

The selector is intentionally side-effect free and only depends on normalized
observation inputs. It returns chosen values, provenance and a minimal conflict map
without mutating input observations.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from trader.domain.market.quality import FieldQualityState, FieldValue
from trader.infra.market_data.service.observations import JsonScalar, SourceObservation

REALTIME_FIELDS = frozenset(
    {
        "name",
        "price",
        "previous_close",
        "open_price",
        "high",
        "low",
        "pct_change",
        "change_5m",
        "speed",
        "volume_ratio",
        "turnover_rate",
        "amount",
        "amplitude",
        "market_cap",
        "is_st",
        "is_suspended",
        "is_one_price_limit",
        "is_blacklisted",
        "has_major_regulatory_risk",
    }
)
BOARD_FIELDS = frozenset(
    {
        "board",
        "exchange",
        "listing_date",
        "listing_age_sessions",
        "is_relisted_first_session",
        "is_delisting_period_first_session",
        "has_price_limit",
        "exchange_limit_pct",
        "strategy_hot_cap_pct",
        "rule_version",
        "rule_effective_date",
    }
)
REALTIME_SOURCES = frozenset({"eastmoney", "sina", "tencent"})
BOARD_SOURCES = frozenset({"exchange", "tushare", "akshare", "eastmoney", "sina", "tencent"})
SOURCE_PRIORITY = {"sina": 1, "eastmoney": 2, "tencent": 3, "akshare": 4, "tushare": 5, "exchange": 6}


@dataclass(frozen=True)
class FieldSelection:
    """Projection output for one symbol's observations."""

    values: Mapping[str, JsonScalar]
    sources: Mapping[str, str]
    selected_observations: Mapping[str, SourceObservation]
    conflicts: tuple[str, ...]
    quality: Mapping[str, FieldQualityState]
    field_values: Mapping[str, FieldValue]


@dataclass
class _SelectionState:
    selected_orders: dict[str, tuple[datetime, datetime, int, str, str, str]]
    selected_observations: dict[str, SourceObservation]
    values: dict[str, JsonScalar]
    sources: dict[str, str]
    quality: dict[str, FieldQualityState]
    field_values: dict[str, FieldValue]
    conflicts: set[str]


def normalize_source(source: str) -> str:
    """Normalize source identifier for deterministic source-level rules."""

    return source.strip().lower().split("_", 1)[0].split("-", 1)[0]


def source_priority(source: str) -> int:
    return SOURCE_PRIORITY.get(normalize_source(source), 0)


def allows_source_for_field(source: str, field: str) -> bool:
    normalized = normalize_source(source)
    if field in REALTIME_FIELDS:
        return normalized in REALTIME_SOURCES
    if field in BOARD_FIELDS:
        return normalized in BOARD_SOURCES
    return True


def field_order(
    observation: SourceObservation,
    *,
    targeted: bool,
    field: str,
) -> tuple[datetime, datetime, int, str, str, str]:
    """Deterministic per-field selection key.

    Tie-breakers include source time, receive time, normalized source priority,
    data version, payload hash, and source name. This keeps input order irrelevant.
    """

    normalized_source = normalize_source(observation.source)
    source_rank = source_priority(normalized_source)
    if field in REALTIME_FIELDS and normalized_source == "tencent" and not targeted:
        source_rank = 0
    return (
        observation.source_time,
        observation.received_at,
        source_rank,
        observation.data_version,
        observation.payload_hash,
        normalized_source,
    )


def select_fields(
    observations: Sequence[SourceObservation],
    *,
    targeted: bool,
) -> FieldSelection:
    """Select one value per field by source/time/priority rule with conflicts."""

    state = _SelectionState(
        selected_orders={},
        selected_observations={},
        values={},
        sources={},
        quality={},
        field_values={},
        conflicts=set(),
    )

    for observation in observations:
        normalized_source = normalize_source(observation.source)
        for field, value in observation.fields.items():
            if value is None or not allows_source_for_field(normalized_source, field):
                continue

            order = field_order(observation, targeted=targeted, field=field)
            current_order = state.selected_orders.get(field)

            if current_order is None or order > current_order:
                _apply_new_selection(
                    field,
                    observation,
                    value,
                    order,
                    state,
                )
                continue

            if order == current_order:
                current_value = state.values[field]
                if current_value != value:
                    state.conflicts.add(f"{field}:conflict")
                    state.quality[field] = FieldQualityState.CONFLICTING
                    conflict_count = state.field_values[field].conflict_count + 1
                    state.field_values[field] = _set_field_state(
                        field,
                        current_value,
                        state.selected_observations[field],
                        FieldQualityState.CONFLICTING,
                        conflict_count=conflict_count,
                    )
            elif state.quality.get(field) == FieldQualityState.CONFLICTING:
                state.quality[field] = FieldQualityState.CONFLICTING
            # Older observations do not replace the winner; keep winner quality unchanged.

    return FieldSelection(
        values=state.values,
        sources=state.sources,
        selected_observations=state.selected_observations,
        conflicts=tuple(sorted(state.conflicts)),
        quality=state.quality,
        field_values=state.field_values,
    )


def _apply_new_selection(
    field_name: str,
    observation: SourceObservation,
    value: JsonScalar,
    order: tuple[datetime, datetime, int, str, str, str],
    state: _SelectionState,
) -> None:
    previous_quality = state.quality.get(field_name)
    previous_field = state.field_values.get(field_name)
    previous_conflict_count = previous_field.conflict_count if previous_field is not None else 0
    normalized_source = normalize_source(observation.source)

    state.selected_orders[field_name] = order
    state.selected_observations[field_name] = observation
    state.values[field_name] = value
    state.sources[field_name] = normalized_source
    selected_state = FieldQualityState.DEGRADED if observation.missing_reasons else FieldQualityState.VALID
    if previous_quality == FieldQualityState.CONFLICTING:
        selected_state = FieldQualityState.CONFLICTING
    state.quality[field_name] = selected_state
    state.field_values[field_name] = _set_field_state(
        field_name,
        value,
        observation,
        selected_state,
        conflict_count=previous_conflict_count,
    )


def _set_field_state(
    field: str,
    value: JsonScalar,
    observation: SourceObservation,
    state: FieldQualityState,
    *,
    conflict_count: int = 0,
) -> FieldValue:
    return FieldValue(
        name=field,
        value=value,
        source=normalize_source(observation.source),
        source_time=observation.source_time,
        received_time=observation.received_at,
        data_version=observation.data_version,
        payload_hash=observation.payload_hash,
        quality=state,
        conflict_count=conflict_count,
    )


__all__ = [
    "FieldSelection",
    "FieldQualityState",
    "FieldValue",
    "REALTIME_FIELDS",
    "BOARD_FIELDS",
    "REALTIME_SOURCES",
    "BOARD_SOURCES",
    "allows_source_for_field",
    "field_order",
    "normalize_source",
    "select_fields",
    "source_priority",
]
