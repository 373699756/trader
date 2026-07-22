"""Pure deterministic merge of source observations into canonical quotes."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime

from trader.application.cache import canonical_json_bytes
from trader.domain.market.models import (
    Board,
    CanonicalMarketSnapshot,
    MarketQuote,
)
from trader.infra.market_data.merge_quote import (
    merge_code,
    observation_order,
    rejection_reason,
    source_name,
    source_priority,
)
from trader.infra.market_data.observations import JsonScalar, SourceObservation


def merge_market_observations(
    observations: Sequence[SourceObservation],
    *,
    observed_at: datetime,
    previous: CanonicalMarketSnapshot | None = None,
    targeted_codes: Sequence[str] = (),
) -> CanonicalMarketSnapshot:
    _require_aware(observed_at, "merge observed_at")
    valid: list[SourceObservation] = []
    degraded: set[str] = set()
    missing: dict[str, str] = {}
    for observation in observations:
        if len(observation.subject_key) != 6 or not observation.subject_key.isdigit():
            degraded.add(f"invalid_subject_key:{observation.subject_key}")
            continue
        reason = rejection_reason(observation, observed_at)
        if reason is not None:
            degraded.add(f"{source_name(observation.source)}:{reason}")
            continue
        valid.append(observation)
        for field, value in observation.missing_reasons.items():
            if field == "cache_refresh":
                degraded.add(f"{source_name(observation.source)}:{value}")
            elif field == "cache_error":
                degraded.add(f"{source_name(observation.source)}:{value}")
            missing[f"{observation.subject_key}.{field}.{source_name(observation.source)}"] = value

    if not valid:
        if previous is not None:
            return replace(
                previous,
                degraded_reasons=tuple(
                    sorted({*previous.degraded_reasons, *degraded, "all_sources_failed:last_valid_snapshot"})
                ),
            )
        return _empty_snapshot(observed_at, degraded or {"all_sources_failed:no_last_valid_snapshot"})

    grouped: dict[str, list[SourceObservation]] = defaultdict(list)
    latest_by_source: dict[str, SourceObservation] = {}
    for observation in valid:
        grouped[observation.subject_key].append(observation)
        source = source_name(observation.source)
        current = latest_by_source.get(source)
        if current is None or observation_order(observation) > observation_order(current):
            latest_by_source[source] = observation
    source_versions = {source: observation.data_version for source, observation in latest_by_source.items()}

    quotes: list[MarketQuote] = []
    field_sources: dict[str, dict[str, str]] = {}
    conflicts: set[str] = set()
    targeted = set(targeted_codes)
    for code in sorted(grouped):
        quote, sources, quote_conflicts = merge_code(code, grouped[code], targeted=code in targeted)
        quotes.append(quote)
        field_sources[code] = sources
        conflicts.update(quote_conflicts)

    if not quotes and previous is not None:
        return replace(
            previous,
            degraded_reasons=tuple(
                sorted({*previous.degraded_reasons, *degraded, "all_sources_failed:last_valid_snapshot"})
            ),
        )
    return _canonical_snapshot(
        observed_at=observed_at,
        quotes=tuple(quotes),
        field_sources=field_sources,
        source_versions=source_versions,
        conflicts=tuple(sorted(conflicts)),
        missing_reasons=missing,
        degraded_reasons=tuple(sorted(degraded)),
    )


def overlay_canonical_snapshot(
    base: CanonicalMarketSnapshot | None,
    overlay: CanonicalMarketSnapshot,
) -> CanonicalMarketSnapshot:
    if base is None:
        return overlay
    quotes = {quote.code: quote for quote in base.quotes}
    overlay_codes: set[str] = set()
    for quote in overlay.quotes:
        current = quotes.get(quote.code)
        if current is None or _overlay_replaces(
            current,
            quote,
            base_observed_at=base.observed_at,
            overlay_observed_at=overlay.observed_at,
        ):
            quotes[quote.code] = quote
            overlay_codes.add(quote.code)
    field_sources = {code: dict(sources) for code, sources in base.field_sources.items()}
    field_sources.update(
        {code: dict(sources) for code, sources in overlay.field_sources.items() if code in overlay_codes}
    )
    source_versions = _merge_source_versions(base, overlay, overlay_codes)
    conflicts = {conflict for conflict in base.conflicts if _conflict_subject(conflict) not in overlay_codes}
    conflicts.update(conflict for conflict in overlay.conflicts if _conflict_subject(conflict) in overlay_codes)
    missing = {key: value for key, value in base.missing_reasons.items() if _missing_subject(key) not in overlay_codes}
    missing.update(
        {key: value for key, value in overlay.missing_reasons.items() if _missing_subject(key) in overlay_codes}
    )
    degraded_reasons = set(base.degraded_reasons)
    if overlay_codes:
        degraded_reasons.update(overlay.degraded_reasons)
    return _canonical_snapshot(
        observed_at=max(base.observed_at, overlay.observed_at),
        quotes=tuple(quotes[code] for code in sorted(quotes)),
        field_sources=field_sources,
        source_versions=source_versions,
        conflicts=tuple(sorted(conflicts)),
        missing_reasons=missing,
        degraded_reasons=tuple(sorted(degraded_reasons)),
    )


def subset_canonical_snapshot(
    snapshot: CanonicalMarketSnapshot,
    codes: Sequence[str],
) -> CanonicalMarketSnapshot:
    selected = set(codes)
    quotes = tuple(quote for quote in snapshot.quotes if quote.code in selected)
    return _canonical_snapshot(
        observed_at=snapshot.observed_at,
        quotes=quotes,
        field_sources={
            code: dict(snapshot.field_sources.get(code, {})) for code in selected if code in snapshot.field_sources
        },
        source_versions=dict(snapshot.source_versions),
        conflicts=tuple(conflict for conflict in snapshot.conflicts if _conflict_subject(conflict) in selected),
        missing_reasons={
            key: value for key, value in snapshot.missing_reasons.items() if _missing_subject(key) in selected
        },
        degraded_reasons=snapshot.degraded_reasons,
    )


def snapshot_payload_hash(snapshot: CanonicalMarketSnapshot) -> str:
    return hashlib.sha256(canonical_json_bytes(snapshot)).hexdigest()


def observation_from_quote(quote: MarketQuote, *, source: str, observed_at: datetime) -> SourceObservation:
    fields: dict[str, JsonScalar] = {
        "name": quote.name,
        "price": quote.price,
        "previous_close": quote.previous_close,
        "open_price": quote.open_price,
        "high": quote.high,
        "low": quote.low,
        "pct_change": quote.pct_change,
        "change_5m": quote.change_5m,
        "speed": quote.speed,
        "volume_ratio": quote.volume_ratio,
        "turnover_rate": quote.turnover_rate,
        "amount": quote.amount,
        "amplitude": quote.amplitude,
        "market_cap": quote.market_cap,
        "industry": quote.industry,
        "is_st": quote.is_st,
        "is_suspended": quote.is_suspended,
        "is_one_price_limit": quote.is_one_price_limit,
        "is_blacklisted": quote.is_blacklisted,
        "has_major_regulatory_risk": quote.has_major_regulatory_risk,
    }
    if quote.board is not Board.UNSUPPORTED:
        fields.update(
            {
                "board": quote.board.value,
                "exchange": quote.exchange,
                "listing_date": quote.listing_date.isoformat() if quote.listing_date is not None else None,
                "listing_age_sessions": float(quote.listing_age_sessions)
                if quote.listing_age_sessions is not None
                else None,
                "is_relisted_first_session": quote.is_relisted_first_session,
                "is_delisting_period_first_session": quote.is_delisting_period_first_session,
                "has_price_limit": quote.has_price_limit,
                "exchange_limit_pct": quote.exchange_limit_pct,
                "strategy_hot_cap_pct": quote.strategy_hot_cap_pct,
                "rule_version": quote.rule_version,
                "rule_effective_date": quote.rule_effective_date.isoformat()
                if quote.rule_effective_date is not None
                else None,
            }
        )
    payload_hash = hashlib.sha256(canonical_json_bytes(fields)).hexdigest()
    return SourceObservation(
        source=source,
        subject_key=quote.code,
        observed_at=observed_at,
        source_time=quote.source_time,
        received_at=quote.received_time,
        effective_at=quote.source_time,
        data_version=quote.data_version,
        fields=fields,
        missing_reasons={},
        payload_hash=payload_hash,
        status="success",
        error_code=None,
    )


def _empty_snapshot(observed_at: datetime, degraded: set[str]) -> CanonicalMarketSnapshot:
    merge_epoch = hashlib.sha256(canonical_json_bytes({"observed_at": observed_at, "quotes": []})).hexdigest()[:24]
    return CanonicalMarketSnapshot(observed_at, merge_epoch, (), {}, {}, (), {}, tuple(sorted(degraded)))


def _canonical_snapshot(
    *,
    observed_at: datetime,
    quotes: tuple[MarketQuote, ...],
    field_sources: dict[str, dict[str, str]],
    source_versions: dict[str, str],
    conflicts: tuple[str, ...],
    missing_reasons: dict[str, str],
    degraded_reasons: tuple[str, ...],
) -> CanonicalMarketSnapshot:
    projection = {
        "observed_at": observed_at,
        "quotes": quotes,
        "field_sources": field_sources,
        "source_versions": source_versions,
        "conflicts": conflicts,
        "missing_reasons": missing_reasons,
    }
    merge_epoch = hashlib.sha256(canonical_json_bytes(projection)).hexdigest()[:24]
    return CanonicalMarketSnapshot(
        observed_at=observed_at,
        merge_epoch=merge_epoch,
        quotes=quotes,
        field_sources=field_sources,
        source_versions=source_versions,
        conflicts=conflicts,
        missing_reasons=missing_reasons,
        degraded_reasons=degraded_reasons,
    )


def _require_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")


def _overlay_replaces(
    current: MarketQuote,
    incoming: MarketQuote,
    *,
    base_observed_at: datetime,
    overlay_observed_at: datetime,
) -> bool:
    current_time = (current.source_time, current.received_time)
    incoming_time = (incoming.source_time, incoming.received_time)
    if incoming_time != current_time:
        return incoming_time > current_time
    incoming_source = source_name(incoming.source)
    current_source = source_name(current.source)
    if incoming_source == current_source:
        if incoming.data_version != current.data_version:
            return incoming.data_version > current.data_version
        current_restrictions = set(current.execution_restrictions)
        incoming_restrictions = set(incoming.execution_restrictions)
        if current_restrictions < incoming_restrictions:
            return True
        if incoming_restrictions < current_restrictions:
            return False
        if overlay_observed_at != base_observed_at:
            return overlay_observed_at > base_observed_at
        return canonical_json_bytes(incoming) > canonical_json_bytes(current)
    return (source_priority(incoming_source), incoming_source) > (
        source_priority(current_source),
        current_source,
    )


def _conflict_subject(conflict: str) -> str:
    return conflict.rpartition(":")[2]


def _missing_subject(key: str) -> str:
    return key.partition(".")[0]


def _merge_source_versions(
    base: CanonicalMarketSnapshot,
    overlay: CanonicalMarketSnapshot,
    overlay_codes: set[str],
) -> dict[str, str]:
    versions = dict(base.source_versions)
    for source, version in overlay.source_versions.items():
        if source not in versions:
            versions[source] = version
            continue
        overlay_order = _source_quote_order(overlay, source, overlay_codes)
        if overlay_order is None:
            continue
        base_order = _source_quote_order(base, source, None)
        if base_order is None or overlay_order > base_order:
            versions[source] = version
    return versions


def _source_quote_order(
    snapshot: CanonicalMarketSnapshot,
    source: str,
    codes: set[str] | None,
) -> tuple[datetime, datetime, str] | None:
    orders = (
        (quote.source_time, quote.received_time, quote.data_version)
        for quote in snapshot.quotes
        if source_name(quote.source) == source and (codes is None or quote.code in codes)
    )
    return max(orders, default=None)


__all__ = [
    "merge_market_observations",
    "observation_from_quote",
    "overlay_canonical_snapshot",
    "snapshot_payload_hash",
    "subset_canonical_snapshot",
]
