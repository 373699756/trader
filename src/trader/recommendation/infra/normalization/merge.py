"""Pure deterministic merge of source observations into canonical quotes."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal, TypedDict, cast

if TYPE_CHECKING:
    from typing_extensions import Unpack

from trader.infra.cache_contracts import canonical_json_bytes
from trader.recommendation.infra.normalization.columnar_merge import (
    ColumnarMergeError,
    try_merge_complete_realtime,
)
from trader.recommendation.infra.normalization.merge_quote import (
    merge_code,
    observation_order,
    rejection_reason,
    source_name,
    source_priority,
)
from trader.infra.market_data.observations import JsonScalar, SourceObservation
from trader.recommendation.domain.market.models import (
    Board,
    CanonicalMarketSnapshot,
    MarketQuote,
)


@dataclass(frozen=True)
class _MergeContext:
    observed_at: datetime
    previous: CanonicalMarketSnapshot | None
    targeted_codes: frozenset[str]
    missing_reasons: dict[str, str]
    degraded_reasons: tuple[str, ...]


def merge_market_observations(
    observations: Sequence[SourceObservation],
    *,
    observed_at: datetime,
    previous: CanonicalMarketSnapshot | None = None,
    targeted_codes: Sequence[str] = (),
    max_age_seconds: float | None = None,
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

    valid, consistency_reasons = _validate_observation_consistency(
        valid, observed_at=observed_at, max_age_seconds=max_age_seconds
    )
    degraded.update(consistency_reasons)
    if not valid:
        if previous is not None:
            return replace(
                previous,
                degraded_reasons=tuple(
                    sorted({*previous.degraded_reasons, *degraded, "all_sources_failed:last_valid_snapshot"})
                ),
                failure_categories=_failure_categories((*previous.failure_categories, *degraded)),
                status="stale",
            )
        return _empty_snapshot(observed_at, degraded or {"all_sources_failed:no_last_valid_snapshot"})

    return _merge_valid_observations(
        valid,
        _MergeContext(
            observed_at=observed_at,
            previous=previous,
            targeted_codes=frozenset(targeted_codes),
            missing_reasons=missing,
            degraded_reasons=tuple(sorted(degraded)),
        ),
    )


def _merge_valid_observations(
    valid: Sequence[SourceObservation],
    context: _MergeContext,
) -> CanonicalMarketSnapshot:
    merge_epoch = _observation_merge_epoch(valid, context)
    if not context.targeted_codes:
        try:
            columnar = try_merge_complete_realtime(valid)
        except ColumnarMergeError:
            columnar = None
            context = replace(
                context,
                degraded_reasons=tuple(sorted({*context.degraded_reasons, "columnar_merge_failed"})),
            )
        if columnar is not None:
            return _canonical_snapshot(
                observed_at=context.observed_at,
                quotes=columnar.quotes,
                field_sources=columnar.field_sources,
                source_versions=columnar.source_versions,
                conflicts=columnar.conflicts,
                missing_reasons=context.missing_reasons,
                degraded_reasons=context.degraded_reasons,
                merge_epoch=merge_epoch,
                source_ages_seconds={
                    source: round(
                        max(0.0, (context.observed_at - observation.source_time).total_seconds()),
                        3,
                    )
                    for source in {source_name(item.source) for item in valid}
                    for observation in [max(
                        (item for item in valid if source_name(item.source) == source),
                        key=observation_order,
                    )]
                },
                failure_categories=_failure_categories((*context.degraded_reasons, *columnar.conflicts)),
                status=_snapshot_status(context.degraded_reasons, columnar.conflicts),
            )

    grouped: dict[str, list[SourceObservation]] = defaultdict(list)
    latest_by_source: dict[str, SourceObservation] = {}
    for observation in valid:
        grouped[observation.subject_key].append(observation)
        source = source_name(observation.source)
        current = latest_by_source.get(source)
        if current is None or observation_order(observation) > observation_order(current):
            latest_by_source[source] = observation
    source_versions = {source: observation.data_version for source, observation in latest_by_source.items()}
    source_ages = {
        source: round(max(0.0, (context.observed_at - observation.source_time).total_seconds()), 3)
        for source, observation in latest_by_source.items()
    }

    quotes: list[MarketQuote] = []
    field_sources: dict[str, dict[str, str]] = {}
    conflicts: set[str] = set()
    for code in sorted(grouped):
        quote, sources, quote_conflicts = merge_code(
            code,
            grouped[code],
            targeted=code in context.targeted_codes,
        )
        quotes.append(quote)
        field_sources[code] = sources
        conflicts.update(quote_conflicts)

    if not quotes and context.previous is not None:
        return replace(
            context.previous,
            degraded_reasons=tuple(
                sorted(
                    {
                        *context.previous.degraded_reasons,
                        *context.degraded_reasons,
                        "all_sources_failed:last_valid_snapshot",
                    }
                )
            ),
            failure_categories=_failure_categories(
                (
                    *context.previous.failure_categories,
                    *context.degraded_reasons,
                    "all_sources_failed:last_valid_snapshot",
                )
            ),
            status="stale",
        )
    return _canonical_snapshot(
        observed_at=context.observed_at,
        quotes=tuple(quotes),
        field_sources=field_sources,
        source_versions=source_versions,
        conflicts=tuple(sorted(conflicts)),
        missing_reasons=context.missing_reasons,
        degraded_reasons=context.degraded_reasons,
        merge_epoch=merge_epoch,
        source_ages_seconds=source_ages,
        failure_categories=_failure_categories((*context.degraded_reasons, *conflicts)),
        status=_snapshot_status(context.degraded_reasons, conflicts),
    )


def _observation_merge_epoch(
    observations: Sequence[SourceObservation],
    context: _MergeContext,
) -> str:
    """Identify the immutable accepted inputs without re-encoding projected quotes."""

    identities = tuple(
        (
            observation.subject_key,
            source_name(observation.source),
            observation.source_time.isoformat(),
            observation.received_at.isoformat(),
            observation.effective_at.isoformat(),
            observation.data_version,
            observation.payload_hash,
        )
        for observation in sorted(
            observations,
            key=lambda item: (
                item.subject_key,
                source_name(item.source),
                observation_order(item),
            ),
        )
    )
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "accepted_observations": identities,
                "missing_reasons": context.missing_reasons,
                "observed_at": context.observed_at,
                "targeted_codes": tuple(sorted(context.targeted_codes)),
            }
        )
    ).hexdigest()[:24]


def overlay_canonical_snapshot(
    base: CanonicalMarketSnapshot | None,
    overlay: CanonicalMarketSnapshot,
) -> CanonicalMarketSnapshot:
    if base is None:
        return overlay
    quotes = list(base.quotes)
    positions = {quote.code: index for index, quote in enumerate(quotes)}
    inserted = False
    overlay_codes: set[str] = set()
    for quote in overlay.quotes:
        position = positions.get(quote.code)
        current = None if position is None else quotes[position]
        if current is None or _overlay_replaces(
            current,
            quote,
            base_observed_at=base.observed_at,
            overlay_observed_at=overlay.observed_at,
        ):
            if position is None:
                positions[quote.code] = len(quotes)
                quotes.append(quote)
                inserted = True
            else:
                quotes[position] = quote
            overlay_codes.add(quote.code)
    if inserted:
        quotes.sort(key=lambda item: item.code)
    field_sources = dict(base.field_sources)
    field_sources.update({code: sources for code, sources in overlay.field_sources.items() if code in overlay_codes})
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
    source_ages = dict(base.source_ages_seconds)
    for source, age in overlay.source_ages_seconds.items():
        if source not in source_ages or overlay_codes:
            source_ages[source] = age
    merged_conflicts = tuple(sorted(conflicts))
    merge_epoch = hashlib.sha256(
        canonical_json_bytes(
            {
                "component_merge_epochs": tuple(sorted((base.merge_epoch, overlay.merge_epoch))),
                "observed_at": max(base.observed_at, overlay.observed_at),
            }
        )
    ).hexdigest()[:24]
    return _canonical_snapshot(
        observed_at=max(base.observed_at, overlay.observed_at),
        quotes=tuple(quotes),
        field_sources=field_sources,
        source_versions=source_versions,
        conflicts=tuple(sorted(conflicts)),
        missing_reasons=missing,
        degraded_reasons=tuple(sorted(degraded_reasons)),
        merge_epoch=merge_epoch,
        source_ages_seconds=source_ages,
        failure_categories=_failure_categories((*degraded_reasons, *merged_conflicts)),
        status=_snapshot_status(degraded_reasons, merged_conflicts),
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
        source_ages_seconds=snapshot.source_ages_seconds,
        failure_categories=snapshot.failure_categories,
        status=snapshot.status,
    )


def snapshot_payload_hash(snapshot: CanonicalMarketSnapshot) -> str:
    # Keep the payload identity stable while operational age/failure metadata evolves.
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "observed_at": snapshot.observed_at,
                "merge_epoch": snapshot.merge_epoch,
                "quotes": snapshot.quotes,
                "field_sources": snapshot.field_sources,
                "source_versions": snapshot.source_versions,
                "conflicts": snapshot.conflicts,
                "missing_reasons": snapshot.missing_reasons,
                "degraded_reasons": snapshot.degraded_reasons,
            }
        )
    ).hexdigest()


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
    return CanonicalMarketSnapshot(
        observed_at=observed_at,
        merge_epoch=merge_epoch,
        quotes=(),
        reference_epoch="reference:unknown",
        field_sources={},
        source_versions={},
        conflicts=(),
        missing_reasons={},
        degraded_reasons=tuple(sorted(degraded)),
        source_ages_seconds={},
        failure_categories=_failure_categories(degraded),
        status="missing",
    )


class _CanonicalSnapshotRequiredOptions(TypedDict):
    observed_at: datetime
    quotes: tuple[MarketQuote, ...]
    field_sources: Mapping[str, Mapping[str, str]]
    source_versions: dict[str, str]
    conflicts: tuple[str, ...]
    missing_reasons: dict[str, str]
    degraded_reasons: tuple[str, ...]


class _CanonicalSnapshotOptionalOptions(TypedDict, total=False):
    merge_epoch: str | None
    reference_epoch: str
    source_ages_seconds: Mapping[str, float]
    failure_categories: tuple[str, ...]
    status: str


class _CanonicalSnapshotOptions(_CanonicalSnapshotRequiredOptions, _CanonicalSnapshotOptionalOptions):
    pass


def _canonical_snapshot(
    **options: Unpack[_CanonicalSnapshotOptions],
) -> CanonicalMarketSnapshot:
    observed_at = options["observed_at"]
    quotes = options["quotes"]
    field_sources = options["field_sources"]
    source_versions = options["source_versions"]
    conflicts = options["conflicts"]
    missing_reasons = options["missing_reasons"]
    degraded_reasons = options["degraded_reasons"]
    merge_epoch = options.get("merge_epoch")
    projection = {
        "observed_at": observed_at,
        "quotes": quotes,
        "field_sources": field_sources,
        "source_versions": source_versions,
        "conflicts": conflicts,
        "missing_reasons": missing_reasons,
    }
    resolved_merge_epoch = merge_epoch or hashlib.sha256(canonical_json_bytes(projection)).hexdigest()[:24]
    return CanonicalMarketSnapshot(
        observed_at=observed_at,
        merge_epoch=resolved_merge_epoch,
        quotes=quotes,
        reference_epoch=options.get("reference_epoch", "reference:unknown"),
        field_sources=field_sources,
        source_versions=source_versions,
        conflicts=conflicts,
        missing_reasons=missing_reasons,
        degraded_reasons=degraded_reasons,
        source_ages_seconds=options.get("source_ages_seconds", {}),
        failure_categories=options.get("failure_categories", _failure_categories(degraded_reasons)),
        status=cast(
            Literal["fresh", "degraded", "stale", "missing", "conflicting"],
            options.get("status", _snapshot_status(degraded_reasons, conflicts)),
        ),
    )


def _validate_observation_consistency(
    observations: Sequence[SourceObservation],
    *,
    observed_at: datetime,
    max_age_seconds: float | None,
) -> tuple[list[SourceObservation], set[str]]:
    if max_age_seconds is not None and max_age_seconds < 0:
        raise ValueError("max_age_seconds must be non-negative")
    grouped: dict[str, list[SourceObservation]] = defaultdict(list)
    for observation in observations:
        grouped[observation.subject_key].append(observation)
    accepted: list[SourceObservation] = []
    reasons: set[str] = set()
    for subject, items in grouped.items():
        realtime_items = [
            item for item in items if item.source.strip().lower() in {"eastmoney", "sina", "tencent"}
        ]
        consistency_items = realtime_items or items
        inconsistent = False
        if len({item.trade_date for item in consistency_items}) > 1:
            reasons.add(f"trade_date_conflict:{subject}")
            inconsistent = True
        if len({item.observation_point.astimezone(timezone.utc) for item in consistency_items if item.observation_point}) > 1:
            reasons.add(f"observation_point_conflict:{subject}")
            inconsistent = True
        identities = {item.security_identity for item in items}
        legacy_rekey = identities != {subject} and all(
            isinstance(identity, str) and len(identity) == 6 and identity.isdigit() for identity in identities
        )
        if identities != {subject} and not legacy_rekey:
            reasons.add(f"security_identity_conflict:{subject}")
            inconsistent = True
        if inconsistent:
            continue
        for item in items:
            # A fixture or replay may carry a historical source timestamp while
            # the observation itself is freshly acquired. Freshness is enforced
            # within the same trade date; cross-date age is surfaced in metadata.
            same_trade_date = item.source_time.astimezone(timezone.utc).date() == observed_at.astimezone(timezone.utc).date()
            age = max(0.0, (observed_at - item.source_time).total_seconds()) if same_trade_date else 0.0
            # Realtime quote deadlines must not evict durable security-reference
            # observations. Reference validity is owned by the reference loader.
            if (
                max_age_seconds is not None
                and source_name(item.source) in {"eastmoney", "sina", "tencent"}
                and age > max_age_seconds
            ):
                reasons.add(f"freshness_expired:{subject}:{source_name(item.source)}")
                continue
            accepted.append(item)
    return accepted, reasons


def _failure_categories(reasons: Iterable[str]) -> tuple[str, ...]:
    categories: set[str] = set()
    for reason in reasons:
        if "freshness_expired" in reason or "last_valid_snapshot" in reason:
            categories.add("stale")
        elif "conflict" in reason or reason.startswith("price_divergence:"):
            categories.add("conflicting")
        elif "failed" in reason or "unavailable" in reason or "no_data" in reason:
            categories.add("failed")
        elif reason:
            categories.add("degraded")
    return tuple(sorted(categories))


def _snapshot_status(reasons: Iterable[str], conflicts: Iterable[str]) -> str:
    if conflicts or any("conflict" in reason or reason.startswith("price_divergence:") for reason in reasons):
        return "conflicting"
    if any("freshness_expired" in reason or "last_valid_snapshot" in reason for reason in reasons):
        return "stale"
    return "degraded" if reasons else "fresh"


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
        return _same_source_overlay_replaces(current, incoming, base_observed_at, overlay_observed_at)
    return (source_priority(incoming_source), incoming_source) > (
        source_priority(current_source),
        current_source,
    )


def _same_source_overlay_replaces(
    current: MarketQuote,
    incoming: MarketQuote,
    base_observed_at: datetime,
    overlay_observed_at: datetime,
) -> bool:
    if incoming.data_version != current.data_version:
        return incoming.data_version > current.data_version
    current_restrictions = set(current.execution_restrictions)
    incoming_restrictions = set(incoming.execution_restrictions)
    if current_restrictions != incoming_restrictions:
        return current_restrictions < incoming_restrictions
    if overlay_observed_at != base_observed_at:
        return overlay_observed_at > base_observed_at
    return canonical_json_bytes(incoming) > canonical_json_bytes(current)


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
