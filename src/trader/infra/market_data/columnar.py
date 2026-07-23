"""Typed Polars batches and deterministic dirty-set projection for P1-P3."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

import polars as pl
from polars.datatypes import DataType, DataTypeClass

from trader.application.cache import canonical_json_bytes
from trader.application.ports import youhua as public_youhua
from trader.domain.market.models import CanonicalMarketSnapshot, FeatureSnapshot, MarketQuote
from trader.domain.market.research import ResearchObservation

_CHANGE_SCHEMA_VERSION = "market_change_set_v1"
_QUOTE_SCHEMA_VERSION = "columnar_quote_batch_v1"
_RESEARCH_SCHEMA_VERSION = "columnar_research_batch_v1"
_FEATURE_SCHEMA_VERSION = "columnar_feature_batch_v1"
_EMPTY_MANIFEST_HASH = hashlib.sha256(canonical_json_bytes(())).hexdigest()
_NO_VERSION = "not_applicable"
_FIELD_FAMILIES: dict[str, str] = {
    "price": "quote_price",
    "pct_change": "quote_price",
    "amount": "quote_liquidity",
    "turnover_rate": "quote_liquidity",
    "data_version": "quote_identity",
    "source_time": "quote_identity",
    "board": "board",
    "industry": "industry",
    "is_st": "risk",
    "is_suspended": "risk",
    "is_one_price_limit": "risk",
    "is_blacklisted": "risk",
    "has_major_regulatory_risk": "risk",
}


@dataclass(frozen=True)
class ColumnarBatchIdentity:
    dataset: str
    merge_epoch: str
    board_policy_version: str
    strategy_version: str
    config_version: str
    schema_version: str
    manifest_hash: str = ""
    content_hash: str = ""

    @property
    def digest(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.__dict__)).hexdigest()


@dataclass(frozen=True)
class MarketChangeSet:
    merge_epoch: str
    inserted_codes: tuple[str, ...]
    updated_codes: tuple[str, ...]
    removed_codes: tuple[str, ...]
    schema_version: str = _CHANGE_SCHEMA_VERSION
    previous_merge_epoch: str | None = None
    dirty_boards: tuple[str, ...] = ()
    dirty_industries: tuple[str, ...] = ()
    dirty_field_families: tuple[str, ...] = ()
    evidence_manifest_hash: str = ""
    risk_changed_codes: tuple[str, ...] = ()
    overlay_only: bool = False
    full_invalidation_reason: str | None = None
    content_hash: str = ""

    @property
    def dirty_codes(self) -> tuple[str, ...]:
        return tuple(sorted((*self.inserted_codes, *self.updated_codes, *self.removed_codes)))

    @property
    def has_full_invalidation(self) -> bool:
        return self.full_invalidation_reason is not None

    def to_public(self) -> public_youhua.MarketChangeSet:
        inserted_codes = () if self.overlay_only else self.inserted_codes
        updated_codes = () if self.overlay_only else self.updated_codes
        removed_codes = () if self.overlay_only else self.removed_codes
        return public_youhua.MarketChangeSet(
            schema_version=public_youhua.MARKET_CHANGE_SET_VERSION,
            merge_epoch=self.merge_epoch,
            previous_merge_epoch=self.previous_merge_epoch,
            inserted_codes=inserted_codes,
            updated_codes=updated_codes,
            removed_codes=removed_codes,
            dirty_codes=self.dirty_codes,
            dirty_boards=self.dirty_boards,
            dirty_industries=self.dirty_industries,
            dirty_field_families=self.dirty_field_families,
            overlay_only=self.overlay_only,
            full_invalidation_reason=self.full_invalidation_reason,
            evidence_manifest_hash=_manifest_hash_or_empty(self.evidence_manifest_hash),
            content_hash=self.content_hash,
        )


@dataclass(frozen=True)
class ColumnarQuoteBatch:
    identity: ColumnarBatchIdentity
    frame: pl.DataFrame

    @classmethod
    def from_snapshot(
        cls,
        snapshot: CanonicalMarketSnapshot,
        *,
        config_version: str,
        schema_version: str,
    ) -> ColumnarQuoteBatch:
        return cls.from_quotes(
            snapshot.quotes,
            merge_epoch=snapshot.merge_epoch,
            config_version=config_version,
            schema_version=schema_version,
        )

    @classmethod
    def from_quotes(
        cls,
        quotes: tuple[MarketQuote, ...],
        *,
        merge_epoch: str,
        config_version: str,
        schema_version: str = _QUOTE_SCHEMA_VERSION,
        manifest_hash: str = "",
    ) -> ColumnarQuoteBatch:
        frame = _strict_frame([_quote_row(quote) for quote in quotes], _QUOTE_SCHEMA).sort("code")
        content_hash = _frame_hash(frame)
        return cls(
            ColumnarBatchIdentity(
                dataset="canonical_market_snapshot",
                merge_epoch=merge_epoch,
                board_policy_version=_NO_VERSION,
                strategy_version=_NO_VERSION,
                config_version=config_version,
                schema_version=schema_version,
                manifest_hash=manifest_hash,
                content_hash=content_hash,
            ),
            frame,
        )


@dataclass(frozen=True)
class ColumnarResearchBatch:
    identity: ColumnarBatchIdentity
    frame: pl.DataFrame

    @classmethod
    def from_observations(
        cls,
        observations: dict[str, ResearchObservation],
        *,
        merge_epoch: str,
        config_version: str,
        schema_version: str = _RESEARCH_SCHEMA_VERSION,
    ) -> ColumnarResearchBatch:
        frame = _strict_frame(
            [_research_row(code, observation) for code, observation in observations.items()],
            _RESEARCH_SCHEMA,
        ).sort("code")
        content_hash = _frame_hash(frame)
        return cls(
            ColumnarBatchIdentity(
                dataset="research_observations",
                merge_epoch=merge_epoch,
                board_policy_version=_NO_VERSION,
                strategy_version=_NO_VERSION,
                config_version=config_version,
                schema_version=schema_version,
                manifest_hash=_evidence_manifest_hash(frame),
                content_hash=content_hash,
            ),
            frame,
        )


@dataclass(frozen=True)
class ColumnarFeatureBatch:
    identity: ColumnarBatchIdentity
    frame: pl.DataFrame

    @classmethod
    def from_features(
        cls,
        features: tuple[FeatureSnapshot, ...],
        options: ColumnarFeatureBatchOptions,
    ) -> ColumnarFeatureBatch:
        frame = _strict_frame(
            [_feature_row(snapshot, options.feature_names) for snapshot in features],
            _feature_schema(options.feature_names),
        ).sort("code")
        content_hash = _frame_hash(frame)
        return cls(
            ColumnarBatchIdentity(
                dataset="feature_snapshots",
                merge_epoch=options.merge_epoch,
                board_policy_version=options.board_policy_version,
                strategy_version=options.strategy_version,
                config_version=options.config_version,
                schema_version=options.feature_schema_version,
                manifest_hash=_evidence_manifest_hash(frame),
                content_hash=content_hash,
            ),
            frame,
        )

    def to_public_envelope(
        self,
        features: tuple[FeatureSnapshot, ...],
        change_set: MarketChangeSet,
        options: FeatureEnvelopeOptions,
    ) -> public_youhua.FeatureSnapshotEnvelope:
        return public_youhua.FeatureSnapshotEnvelope(
            schema_version=public_youhua.P3_P4_SCHEMA_VERSION,
            snapshot_version=options.snapshot_version or self.identity.digest,
            feature_snapshot_version=options.feature_snapshot_version or self.identity.schema_version,
            trade_date=options.trade_date,
            phase=options.phase,
            merge_epoch=self.identity.merge_epoch,
            data_version=options.data_version,
            config_version=self.identity.config_version,
            feature_schema_version=self.identity.schema_version,
            content_hash=self.identity.content_hash,
            feature_snapshots=tuple(sorted(features, key=lambda snapshot: snapshot.quote.code)),
            market_change_set=change_set.to_public(),
        )


@dataclass(frozen=True)
class ColumnarFeatureBatchOptions:
    merge_epoch: str
    config_version: str
    feature_schema_version: str
    feature_names: tuple[str, ...]
    strategy_version: str = _NO_VERSION
    board_policy_version: str = _NO_VERSION


@dataclass(frozen=True)
class FeatureEnvelopeOptions:
    trade_date: str
    phase: str
    data_version: str
    snapshot_version: str | None = None
    feature_snapshot_version: str | None = None


def market_changes(
    previous: ColumnarQuoteBatch | None,
    current: ColumnarQuoteBatch,
) -> MarketChangeSet:
    if previous is None or previous.frame.is_empty():
        inserted = _sorted_strings(current.frame.get_column("code").to_list())
        return MarketChangeSet(
            current.identity.merge_epoch,
            inserted,
            (),
            (),
            previous_merge_epoch=None,
            dirty_boards=_dimension_values(current.frame, "board", inserted),
            dirty_industries=_dimension_values(current.frame, "industry", inserted),
            dirty_field_families=tuple(sorted(set(_FIELD_FAMILIES.values()))),
            evidence_manifest_hash=_manifest_hash_or_empty(current.identity.manifest_hash),
            overlay_only=_overlay_only(tuple(sorted(set(_FIELD_FAMILIES.values())))),
            content_hash=current.identity.content_hash,
        )
    comparable = tuple(_FIELD_FAMILIES)
    old = previous.frame.select("code", *comparable)
    new = current.frame.select("code", *comparable)
    joined = new.join(old, on="code", how="full", suffix="_old", coalesce=True)
    raw_inserted = joined.filter(pl.col("data_version_old").is_null()).get_column("code").to_list()
    old_codes = set(old.get_column("code").to_list())
    new_codes = set(new.get_column("code").to_list())
    shared = joined.filter(pl.col("data_version").is_not_null() & pl.col("data_version_old").is_not_null())
    changed_expr = pl.any_horizontal(*[pl.col(name).ne_missing(pl.col(f"{name}_old")) for name in comparable])
    updated = shared.filter(changed_expr).get_column("code").to_list()
    inserted_codes = _sorted_strings(raw_inserted)
    updated_codes = _sorted_strings(updated)
    removed_codes = _sorted_strings(old_codes - new_codes)
    dirty_codes = tuple(sorted((*inserted_codes, *updated_codes, *removed_codes)))
    dirty_families = _dirty_field_families(shared, comparable)
    if inserted_codes or removed_codes:
        dirty_families = tuple(sorted(set(_FIELD_FAMILIES.values())))
    full_invalidation_reason = None
    if previous.identity.schema_version != current.identity.schema_version:
        full_invalidation_reason = "schema_version_changed"
    elif previous.identity.config_version != current.identity.config_version:
        full_invalidation_reason = "config_version_changed"
    return MarketChangeSet(
        current.identity.merge_epoch,
        inserted_codes,
        updated_codes,
        removed_codes,
        previous_merge_epoch=previous.identity.merge_epoch,
        dirty_boards=_dimension_values_across(previous.frame, current.frame, "board", dirty_codes),
        dirty_industries=_dimension_values_across(previous.frame, current.frame, "industry", dirty_codes),
        dirty_field_families=dirty_families,
        evidence_manifest_hash=_manifest_hash_or_empty(current.identity.manifest_hash),
        risk_changed_codes=tuple(sorted({*inserted_codes, *removed_codes, *_risk_changed_codes(shared)})),
        overlay_only=_overlay_only(dirty_families),
        full_invalidation_reason=full_invalidation_reason,
        content_hash=current.identity.content_hash,
    )


_QUOTE_SCHEMA: dict[str, DataTypeClass | DataType] = {
    "code": pl.String,
    "name": pl.String,
    "price": pl.Float64,
    "pct_change": pl.Float64,
    "amount": pl.Float64,
    "turnover_rate": pl.Float64,
    "board": pl.Enum(["main", "chinext", "star", "unsupported"]),
    "industry": pl.String,
    "source": pl.String,
    "data_version": pl.String,
    "source_time": pl.Datetime(time_unit="us", time_zone="UTC"),
    "received_time": pl.Datetime(time_unit="us", time_zone="UTC"),
    "is_st": pl.Boolean,
    "is_suspended": pl.Boolean,
    "is_one_price_limit": pl.Boolean,
    "is_blacklisted": pl.Boolean,
    "has_major_regulatory_risk": pl.Boolean,
    "cross_source_deviation_pct": pl.Float64,
    "cross_source_verified": pl.Boolean,
}

_RESEARCH_SCHEMA: dict[str, DataTypeClass | DataType] = {
    "code": pl.String,
    "announcements_available": pl.Boolean,
    "pledge_ratio_pct": pl.Float64,
    "unlock_ratio_pct": pl.Float64,
    "evidence_count": pl.Int64,
    "source_error_count": pl.Int64,
    "evidence_manifest_hash": pl.String,
}


def _quote_row(
    quote: MarketQuote,
) -> tuple[
    str,
    str,
    float | None,
    float | None,
    float | None,
    float | None,
    str,
    str,
    str,
    str,
    datetime,
    datetime,
    bool,
    bool,
    bool,
    bool,
    bool,
    float | None,
    bool,
]:
    return (
        quote.code,
        quote.name,
        quote.price,
        quote.pct_change,
        quote.amount,
        quote.turnover_rate,
        quote.board.value,
        quote.industry,
        quote.source,
        quote.data_version,
        quote.source_time,
        quote.received_time,
        quote.is_st,
        quote.is_suspended,
        quote.is_one_price_limit,
        quote.is_blacklisted,
        quote.has_major_regulatory_risk,
        quote.cross_source_deviation_pct,
        quote.cross_source_verified,
    )


def _research_row(
    code: str,
    observation: ResearchObservation,
) -> tuple[str, bool, float | None, float | None, int, int, str]:
    evidence_hash = hashlib.sha256(canonical_json_bytes(observation.evidence)).hexdigest()
    return (
        code,
        observation.announcements_available,
        observation.pledge_ratio_pct,
        observation.unlock_ratio_pct,
        len(observation.evidence),
        len(observation.source_errors),
        evidence_hash,
    )


def _feature_schema(feature_names: tuple[str, ...]) -> dict[str, DataTypeClass | DataType]:
    schema: dict[str, DataTypeClass | DataType] = {
        "code": pl.String,
        "board": pl.Enum(["main", "chinext", "star", "unsupported"]),
        "industry": pl.String,
        "observed_at": pl.Datetime(time_unit="us", time_zone="UTC"),
        "market_regime": pl.Enum(["risk_on", "neutral", "risk_off"]),
        "history_days": pl.Int64,
        "missing_count": pl.Int64,
        "evidence_count": pl.Int64,
        "board_data_reliability": pl.Float64,
        "board_supported_weight": pl.Float64,
        "competition_group_id": pl.String,
        "liquidity_bucket": pl.String,
        "evidence_manifest_hash": pl.String,
    }
    schema.update({name: pl.Float64 for name in feature_names})
    return schema


def _feature_row(snapshot: FeatureSnapshot, feature_names: tuple[str, ...]) -> tuple[object, ...]:
    evidence_hash = hashlib.sha256(canonical_json_bytes(snapshot.evidence)).hexdigest()
    return (
        snapshot.quote.code,
        snapshot.quote.board.value,
        snapshot.quote.industry,
        snapshot.observed_at,
        snapshot.market_regime,
        snapshot.history_days,
        len(snapshot.missing_fields),
        len(snapshot.evidence),
        snapshot.board_data_reliability,
        snapshot.board_supported_weight,
        snapshot.competition_group_id,
        snapshot.liquidity_bucket,
        evidence_hash,
        *(snapshot.values.get(name) for name in feature_names),
    )


def _strict_frame(rows: list[tuple[object, ...]], schema: dict[str, DataTypeClass | DataType]) -> pl.DataFrame:
    frame = pl.DataFrame(rows, schema=schema, orient="row")
    object_columns = [name for name, dtype in frame.schema.items() if dtype == pl.Object]
    if object_columns:
        joined = ",".join(object_columns)
        raise TypeError(f"columnar batches must not contain Object columns: {joined}")
    return frame


def _frame_hash(frame: pl.DataFrame) -> str:
    return hashlib.sha256(canonical_json_bytes(frame.to_dicts())).hexdigest()


def _evidence_manifest_hash(frame: pl.DataFrame) -> str:
    if "evidence_manifest_hash" not in frame.columns:
        return ""
    values = tuple(sorted(str(value) for value in frame.get_column("evidence_manifest_hash").to_list()))
    return hashlib.sha256(canonical_json_bytes(values)).hexdigest()


def _manifest_hash_or_empty(value: str) -> str:
    return value or _EMPTY_MANIFEST_HASH


def _sorted_strings(values: Iterable[object]) -> tuple[str, ...]:
    return tuple(sorted(str(value) for value in values))


def _dimension_values(frame: pl.DataFrame, column: str, codes: tuple[str, ...]) -> tuple[str, ...]:
    if not codes or column not in frame.columns:
        return ()
    values = (
        frame.filter(pl.col("code").is_in(codes))
        .select(column)
        .drop_nulls()
        .unique()
        .sort(column)
        .get_column(column)
        .to_list()
    )
    return _sorted_strings(values)


def _dimension_values_across(
    previous: pl.DataFrame,
    current: pl.DataFrame,
    column: str,
    codes: tuple[str, ...],
) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                *_dimension_values(previous, column, codes),
                *_dimension_values(current, column, codes),
            }
        )
    )


def _dirty_field_families(shared: pl.DataFrame, comparable: tuple[str, ...]) -> tuple[str, ...]:
    families: set[str] = set()
    for name in comparable:
        if shared.filter(pl.col(name).ne_missing(pl.col(f"{name}_old"))).height:
            families.add(_FIELD_FAMILIES[name])
    return tuple(sorted(families))


def _risk_changed_codes(shared: pl.DataFrame) -> tuple[str, ...]:
    risk_fields = tuple(name for name, family in _FIELD_FAMILIES.items() if family == "risk")
    if not risk_fields:
        return ()
    changed = shared.filter(
        pl.any_horizontal(*[pl.col(name).ne_missing(pl.col(f"{name}_old")) for name in risk_fields])
    )
    return _sorted_strings(changed.get_column("code").to_list())


def _overlay_only(families: tuple[str, ...]) -> bool:
    return bool(families) and set(families).issubset({"quote_price", "quote_liquidity", "quote_identity"})


__all__ = [
    "ColumnarBatchIdentity",
    "ColumnarFeatureBatch",
    "ColumnarFeatureBatchOptions",
    "ColumnarQuoteBatch",
    "ColumnarResearchBatch",
    "FeatureEnvelopeOptions",
    "MarketChangeSet",
    "market_changes",
]
