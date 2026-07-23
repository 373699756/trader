"""Columnar fast path for complete Eastmoney/Sina full-market rows."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol, cast

import polars as pl

from trader.application.cache import canonical_json_bytes
from trader.domain.market.models import Board, MarketQuote
from trader.domain.recommendation.filters import board_for_code
from trader.infra.market_data.merge_quote import observation_order, source_name
from trader.infra.market_data.observations import JsonScalar, SourceObservation

_QUOTE_FIELDS = (
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
    "industry",
    "is_st",
    "is_suspended",
    "is_one_price_limit",
    "is_blacklisted",
    "has_major_regulatory_risk",
)
_SOURCE_PRIORITY = {"sina": 1, "eastmoney": 2}
_RULE_EFFECTIVE_DATE = date(2023, 8, 28)
_DEGRADED_RESTRICTIONS = ("board_identity_degraded", "missing_listing_date")
_FIELD_SOURCE_TEMPLATES = {
    source: {
        **{field: source for field in _QUOTE_FIELDS},
        "board": "code_prefix_fallback",
        "board_reliability": "code_prefix_fallback",
        "rule_effective_date": "local_rule",
        "rule_version": "local_rule",
        "strategy_hot_cap_pct": "local_rule",
    }
    for source in _SOURCE_PRIORITY
}
_COLUMNAR_INPUT_SCHEMA: dict[str, pl.DataType] = {
    "code": pl.String,
    "name": pl.String,
    "price": pl.Float64,
    "previous_close": pl.Float64,
    "open_price": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "pct_change": pl.Float64,
    "change_5m": pl.Float64,
    "speed": pl.Float64,
    "volume_ratio": pl.Float64,
    "turnover_rate": pl.Float64,
    "amount": pl.Float64,
    "amplitude": pl.Float64,
    "market_cap": pl.Float64,
    "industry": pl.String,
    "is_st": pl.Boolean,
    "is_suspended": pl.Boolean,
    "is_one_price_limit": pl.Boolean,
    "is_blacklisted": pl.Boolean,
    "has_major_regulatory_risk": pl.Boolean,
}


@dataclass(frozen=True)
class ColumnarMarketProjection:
    quotes: tuple[MarketQuote, ...]
    field_sources: dict[str, dict[str, str]]
    source_versions: dict[str, str]
    conflicts: tuple[str, ...]


@dataclass(frozen=True)
class CompleteRealtimeNormalization:
    source: str
    observed_at: datetime
    source_time: datetime
    received_at: datetime
    data_version: str
    price_multiplier: float = 1.0


@dataclass(frozen=True)
class _QuoteProjection:
    source: str
    board: Board
    deviation: float | None
    verified: bool
    restrictions: tuple[str, ...]


class ColumnarMergeError(RuntimeError):
    """The eligible columnar merge failed and must degrade to scalar."""


def try_normalize_complete_realtime_rows(
    rows: Sequence[Mapping[str, object]],
    options: CompleteRealtimeNormalization,
) -> tuple[SourceObservation, ...] | None:
    """Normalize a complete provider batch with typed column expressions.

    Returning ``None`` is an explicit signal to use the general provider
    normalizer for partial, malformed or unsupported payload shapes.
    """

    try:
        return _normalize_complete_realtime_rows(
            rows,
            options,
        )
    except pl.exceptions.PolarsError as exc:
        raise ColumnarMergeError("columnar merge projection failed") from exc
    except (KeyError, OverflowError, TypeError, ValueError):
        return None


def _normalize_complete_realtime_rows(
    rows: Sequence[Mapping[str, object]],
    options: CompleteRealtimeNormalization,
) -> tuple[SourceObservation, ...]:
    normalized_source = options.source.strip()
    normalized_version = options.data_version.strip()
    if source_name(normalized_source) not in _SOURCE_PRIORITY or not normalized_version:
        raise ValueError("complete realtime source and data version must be supported")
    if not math.isfinite(options.price_multiplier):
        raise ValueError("complete realtime price multiplier must be finite")
    for value in (options.observed_at, options.source_time, options.received_at):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("complete realtime timestamps must be timezone-aware")
    if not rows:
        return ()

    columns = {
        name: [row.get(name, False) if _COLUMNAR_INPUT_SCHEMA[name] == pl.Boolean else row.get(name) for row in rows]
        for name in _COLUMNAR_INPUT_SCHEMA
    }
    frame = pl.DataFrame(columns, schema=_COLUMNAR_INPUT_SCHEMA, strict=True)
    if any(frame.null_count().row(0)):
        raise ValueError("complete realtime rows must not contain null fields")
    valid_codes = frame.select(pl.col("code").str.contains(r"^\d{6}$").all()).item()
    if valid_codes is not True:
        raise ValueError("complete realtime rows contain invalid codes")

    special_board = (
        pl.col("code").str.starts_with("300")
        | pl.col("code").str.starts_with("301")
        | pl.col("code").str.starts_with("688")
        | pl.col("code").str.starts_with("689")
    )
    limit_threshold = pl.when(special_board).then(pl.lit(19.5)).otherwise(pl.lit(9.5))
    frame = frame.with_columns((pl.col("price") * options.price_multiplier).alias("price")).with_columns(
        pl.max_horizontal("price", "high").alias("high")
    )
    inferred_one_price = (
        (pl.col("price") > 0)
        & ((pl.col("high") - pl.col("low")).abs() < 1e-9)
        & (pl.col("pct_change").abs() >= limit_threshold)
    )
    frame = frame.with_columns((pl.col("is_one_price_limit") | inferred_one_price).alias("is_one_price_limit"))

    observations: list[SourceObservation] = []
    for row in frame.select("code", *_QUOTE_FIELDS).iter_rows(named=False):
        code, *raw_values = row
        fields = dict(zip(_QUOTE_FIELDS, raw_values, strict=True))
        observations.append(
            SourceObservation(
                source=normalized_source,
                subject_key=cast(str, code),
                observed_at=options.observed_at,
                source_time=options.source_time,
                received_at=options.received_at,
                effective_at=options.source_time,
                data_version=normalized_version,
                fields=fields,
                missing_reasons={},
                payload_hash=hashlib.sha256(canonical_json_bytes(fields)).hexdigest(),
                status="success",
                error_code=None,
            )
        )
    return tuple(observations)


def columnar_merge_epoch(
    observed_at: datetime,
    projection: ColumnarMarketProjection,
    missing_reasons: dict[str, str],
) -> str:
    """Hash the canonical projection while reusing repeated source templates."""

    digest = hashlib.sha256()
    digest.update(b'{"conflicts":')
    digest.update(canonical_json_bytes(projection.conflicts))
    digest.update(b',"field_sources":')
    _update_field_sources_hash(digest, projection.field_sources)
    digest.update(b',"missing_reasons":')
    digest.update(canonical_json_bytes(missing_reasons))
    digest.update(b',"observed_at":')
    digest.update(canonical_json_bytes(observed_at))
    digest.update(b',"quotes":')
    digest.update(canonical_json_bytes(projection.quotes))
    digest.update(b',"source_versions":')
    digest.update(canonical_json_bytes(projection.source_versions))
    digest.update(b"}")
    return digest.hexdigest()[:24]


def _update_field_sources_hash(
    digest: AnyHash,
    field_sources: dict[str, dict[str, str]],
) -> None:
    digest.update(b"{")
    encoded_templates: dict[tuple[tuple[str, str], ...], bytes] = {}
    for index, code in enumerate(sorted(field_sources)):
        if index:
            digest.update(b",")
        digest.update(canonical_json_bytes(code))
        digest.update(b":")
        template_key = tuple(sorted(field_sources[code].items()))
        encoded = encoded_templates.get(template_key)
        if encoded is None:
            encoded = canonical_json_bytes(field_sources[code])
            encoded_templates[template_key] = encoded
        digest.update(encoded)
    digest.update(b"}")


class AnyHash(Protocol):
    def update(self, value: bytes, /) -> object: ...


def try_merge_complete_realtime(
    observations: Sequence[SourceObservation],
) -> ColumnarMarketProjection | None:
    """Return an equivalent columnar projection when every provider row is complete.

    The fast path is deliberately narrow. Partial rows, reference metadata,
    Tencent overlays and source degradation retain the general field-level
    scalar merge so no missing-value or board precedence rule is weakened.
    """

    try:
        return _merge_complete_realtime(observations)
    except pl.exceptions.PolarsError as exc:
        raise ColumnarMergeError("columnar merge projection failed") from exc
    except (KeyError, OverflowError, TypeError, ValueError):
        return None


def _merge_complete_realtime(
    observations: Sequence[SourceObservation],
) -> ColumnarMarketProjection | None:
    if not observations:
        return None
    sources = tuple(source_name(observation.source) for observation in observations)
    if set(sources) != set(_SOURCE_PRIORITY):
        return None
    if any(
        observation.status != "success" or len(observation.fields) != len(_QUOTE_FIELDS) or observation.missing_reasons
        for observation in observations
    ):
        return None
    source_codes = {
        source: {
            observation.subject_key
            for observation, normalized in zip(observations, sources, strict=True)
            if normalized == source
        }
        for source in _SOURCE_PRIORITY
    }
    if not source_codes["eastmoney"] or source_codes["eastmoney"] != source_codes["sina"]:
        return None
    identities = {(observation.subject_key, source) for observation, source in zip(observations, sources, strict=True)}
    if len(identities) != len(observations):
        return None

    winners = _winner_indexes(observations, sources)
    prices_by_code: dict[str, list[float]] = {}
    for observation in observations:
        price = _number(observation.fields["price"])
        if price is not None and price > 0:
            prices_by_code.setdefault(observation.subject_key, []).append(price)

    quotes: list[MarketQuote] = []
    field_sources: dict[str, dict[str, str]] = {}
    conflicts: set[str] = set()
    for winner_index in winners:
        selected = observations[winner_index]
        code = selected.subject_key
        selected_source = source_name(selected.source)
        deviation = _price_deviation(prices_by_code.get(code, ()))
        verified = deviation is None or deviation <= 0.5
        board = board_for_code(code)
        restrictions: tuple[str, ...] = _DEGRADED_RESTRICTIONS
        if not verified:
            restrictions = tuple(sorted((*restrictions, "cross_source_deviation")))
            conflicts.add(f"price_divergence:{code}")
        quotes.append(
            _quote_from_complete_observation(
                selected,
                _QuoteProjection(selected_source, board, deviation, verified, restrictions),
            )
        )
        field_sources[code] = dict(_FIELD_SOURCE_TEMPLATES[selected_source])

    source_versions = {
        source: max(
            (
                observation
                for observation, normalized in zip(observations, sources, strict=True)
                if normalized == source
            ),
            key=observation_order,
        ).data_version
        for source in _SOURCE_PRIORITY
    }
    return ColumnarMarketProjection(
        quotes=tuple(quotes),
        field_sources=field_sources,
        source_versions=source_versions,
        conflicts=tuple(sorted(conflicts)),
    )


def _winner_indexes(observations: Sequence[SourceObservation], sources: tuple[str, ...]) -> tuple[int, ...]:
    frame = pl.DataFrame(
        {
            "code": [observation.subject_key for observation in observations],
            "source_time": [observation.source_time for observation in observations],
            "received_time": [observation.received_at for observation in observations],
            "source_priority": [_SOURCE_PRIORITY[source] for source in sources],
            "data_version": [observation.data_version for observation in observations],
            "payload_hash": [observation.payload_hash for observation in observations],
            "row_index": range(len(observations)),
        },
        schema={
            "code": pl.String,
            "source_time": pl.Datetime(time_unit="us", time_zone="UTC"),
            "received_time": pl.Datetime(time_unit="us", time_zone="UTC"),
            "source_priority": pl.Int64,
            "data_version": pl.String,
            "payload_hash": pl.String,
            "row_index": pl.Int64,
        },
    )
    return tuple(
        frame.sort(
            "code",
            "source_time",
            "received_time",
            "source_priority",
            "data_version",
            "payload_hash",
        )
        .unique(subset="code", keep="last", maintain_order=True)
        .sort("code")
        .get_column("row_index")
        .to_list()
    )


def _quote_from_complete_observation(
    observation: SourceObservation,
    projection: _QuoteProjection,
) -> MarketQuote:
    values = observation.fields
    raw_numbers = (
        values["price"],
        values["previous_close"],
        values["open_price"],
        values["high"],
        values["low"],
        values["pct_change"],
        values["change_5m"],
        values["speed"],
        values["volume_ratio"],
        values["turnover_rate"],
        values["amount"],
        values["amplitude"],
        values["market_cap"],
    )
    if any(type(value) is not float for value in raw_numbers):
        raise TypeError("complete quote numeric fields must be floats")
    (
        price,
        previous_close,
        open_price,
        high,
        low,
        pct_change,
        change_5m,
        speed,
        volume_ratio,
        turnover_rate,
        amount,
        amplitude,
        market_cap,
    ) = cast(
        tuple[float, float, float, float, float, float, float, float, float, float, float, float, float], raw_numbers
    )
    raw_booleans = (
        values["is_st"],
        values["is_suspended"],
        values["is_one_price_limit"],
        values["is_blacklisted"],
        values["has_major_regulatory_risk"],
    )
    if any(type(value) is not bool for value in raw_booleans):
        raise TypeError("complete quote boolean fields must be booleans")
    is_st, is_suspended, is_one_price_limit, is_blacklisted, has_major_regulatory_risk = cast(
        tuple[bool, bool, bool, bool, bool], raw_booleans
    )
    return MarketQuote(
        observation.subject_key,
        _required_text(values["name"]),
        price,
        previous_close,
        open_price,
        high,
        low,
        pct_change,
        change_5m,
        speed,
        volume_ratio,
        turnover_rate,
        amount,
        amplitude,
        market_cap,
        _required_text(values["industry"]),
        projection.source,
        observation.source_time,
        observation.received_at,
        observation.data_version,
        is_st,
        is_suspended,
        is_one_price_limit,
        is_blacklisted,
        has_major_regulatory_risk,
        round(projection.deviation, 6) if projection.deviation is not None else None,
        projection.verified,
        projection.board,
        "code_prefix_fallback",
        "degraded",
        "",
        None,
        None,
        None,
        None,
        None,
        None,
        8.0 if projection.board is Board.MAIN else 16.0,
        "cn-board-rules-v1",
        _RULE_EFFECTIVE_DATE,
        projection.restrictions,
    )


def _required_text(value: JsonScalar) -> str:
    if not isinstance(value, str):
        raise TypeError("complete quote text fields must be strings")
    return value


def _price_deviation(prices: Sequence[float]) -> float | None:
    if len(prices) < 2:
        return None
    baseline = Decimal(str(min(prices)))
    maximum = Decimal(str(max(prices)))
    return float((maximum - baseline) / baseline * Decimal("100"))


def _number(value: JsonScalar) -> float | None:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


__all__ = [
    "ColumnarMarketProjection",
    "ColumnarMergeError",
    "CompleteRealtimeNormalization",
    "columnar_merge_epoch",
    "try_merge_complete_realtime",
    "try_normalize_complete_realtime_rows",
]
